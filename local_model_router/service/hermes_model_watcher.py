"""Follow recent Hermes sessions and safely swap one managed GPU slot."""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import socket
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import aiohttp
import yaml

logger = logging.getLogger("uvicorn.error").getChild("imperium.hermes_gpu")

_HERMES_TO_PROFILE = {
    "ornith": "ornith",
    "local": "ornith",
    "uncensored": "qwen38_27b",
    "qwen38_27b": "qwen38_27b",
}
_NON_RETRYABLE_START_CODES = {
    "invalid_slot_name",
    "model_missing",
    "port_in_use",
    "process_identity_changed",
    "process_ownership_unverified",
    "state_write_failed",
}

PinFn = Callable[[str], None]
ManagerFactory = Callable[[], Any]
InspectFn = Callable[[], dict[int, str]]
ProbeFn = Callable[[int], dict[str, Any] | Awaitable[dict[str, Any]]]
PublishFn = Callable[[str, str, Optional[int]], None]
ClearFn = Callable[[str], None]
SleepFn = Callable[[float], Awaitable[None]]


def normalize_hermes_model(name: str) -> Optional[str]:
    raw = str(name or "").strip().lower()
    if not raw or "/" in raw:
        return None
    return _HERMES_TO_PROFILE.get(raw)


def decide_swap(desired: Optional[str], loaded: Optional[str], swapping: bool) -> str:
    if swapping:
        return "busy"
    if not desired or desired == loaded:
        return "idle"
    return "swap"


def pick_session_model(rows: list[dict[str, Any]]) -> Optional[str]:
    """Return the newest open, non-archived session with a local GPU model."""
    ranked = sorted(
        rows,
        key=lambda row: _timestamp_epoch(
            row.get("last_activity_at") or row.get("started_at")
        ),
        reverse=True,
    )
    for row in ranked:
        if row.get("ended_at") not in (None, "", 0) or int(row.get("archived") or 0):
            continue
        mapped = normalize_hermes_model(str(row.get("model") or ""))
        if mapped:
            return mapped
        cfg = row.get("model_config")
        if isinstance(cfg, str) and cfg.strip():
            try:
                parsed = json.loads(cfg)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                mapped = normalize_hermes_model(str(parsed.get("model") or ""))
                if mapped:
                    return mapped
    return None


def _timestamp_epoch(value: Any) -> float:
    """Normalize Hermes REAL epochs and ISO-8601 timestamps for ordering."""
    if isinstance(value, bool) or value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    raw = str(value).strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def default_model_from_yaml(text: str) -> Optional[str]:
    data = yaml.safe_load(text) or {}
    model = data.get("model") if isinstance(data, dict) else None
    if isinstance(model, dict):
        mapped = normalize_hermes_model(str(model.get("default") or ""))
        if mapped:
            return mapped
    provider = (data.get("providers") or {}).get("lmm-router") if isinstance(data, dict) else None
    if isinstance(provider, dict):
        return normalize_hermes_model(str(provider.get("default_model") or ""))
    return None


def match_loaded_profile(hint: str, profiles: dict[str, dict[str, Any]]) -> Optional[str]:
    text = str(hint or "").strip().lower()
    if not text:
        return None
    mapped = normalize_hermes_model(text)
    if mapped in profiles:
        return mapped
    if text in profiles:
        return text
    for profile_id, profile in profiles.items():
        path = str(profile.get("model_path") or "").replace("\\", "/").lower()
        stem = Path(path).stem.lower()
        model_id = str(profile.get("model_id") or "").lower()
        if text in {stem, model_id, profile_id.lower()} or (stem and stem in text):
            return profile_id
    return None


def public_hermes_model_ids() -> tuple[str, ...]:
    return ("ornith", "uncensored", "local")


def pin_extra_ids(profiles: dict[str, dict[str, Any]]) -> set[str]:
    ids = set(public_hermes_model_ids())
    ids.update(profiles)
    for profile in profiles.values():
        model_id = str(profile.get("model_id") or "").strip()
        if model_id:
            ids.add(model_id)
    return ids


@dataclass
class HermesGpuConfig:
    slot_id: str
    poll_seconds: float
    hermes_home: Path
    profiles: dict[str, dict[str, Any]]
    path: Path
    config_path: Path
    active_window_seconds: float = 900.0
    session_limit: int = 32
    drain_notice_seconds: float = 60.0
    max_start_attempts: int = 3
    retry_backoff_seconds: tuple[float, ...] = (15.0, 60.0)
    default_profile: str = "ornith"

    @property
    def state_path(self) -> Path:
        return self.config_path.parent / "hermes_gpu_state.json"

    def profile_for_model(self, model: Optional[str]) -> Optional[str]:
        mapped = normalize_hermes_model(str(model or ""))
        if mapped in self.profiles:
            return mapped
        return match_loaded_profile(str(model or ""), self.profiles)

    def default_max_tokens(self, model: Optional[str]) -> Optional[int]:
        profile_id = self.profile_for_model(model)
        value = (self.profiles.get(profile_id or "") or {}).get("default_max_tokens")
        try:
            return max(1, int(value)) if value is not None else None
        except (TypeError, ValueError):
            return None


def default_hermes_home() -> Path:
    override = os.environ.get("HERMES_HOME")
    if override:
        return Path(override)
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        return Path(local_app) / "hermes"
    return Path.home() / "AppData" / "Local" / "hermes"


def load_hermes_gpu_config(path: Path, fleet_config_path: str | os.PathLike[str]) -> Optional[HermesGpuConfig]:
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        return None
    home = Path(data["hermes_home"]) if data.get("hermes_home") else default_hermes_home()

    def number(key: str, default: float, minimum: float) -> float:
        try:
            return max(minimum, float(data.get(key, default)))
        except (TypeError, ValueError):
            return default

    def integer(key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(maximum, int(data.get(key, default))))
        except (TypeError, ValueError):
            return default

    raw_backoff = data.get("retry_backoff_seconds", [15, 60])
    try:
        backoff = (
            tuple(max(0.0, float(item)) for item in raw_backoff)
            if isinstance(raw_backoff, list)
            else (15.0, 60.0)
        )
    except (TypeError, ValueError):
        backoff = (15.0, 60.0)
    default_profile = str(data.get("default_profile") or "ornith")
    if default_profile not in profiles:
        default_profile = next(iter(profiles))
    return HermesGpuConfig(
        slot_id=str(data.get("slot_id") or "slot_chat"),
        poll_seconds=number("poll_seconds", 3.0, 1.0),
        active_window_seconds=number("active_window_seconds", 900.0, 1.0),
        session_limit=integer("session_limit", 32, 1, 32),
        drain_notice_seconds=number("drain_notice_seconds", 60.0, 0.0),
        max_start_attempts=integer("max_start_attempts", 3, 1, 3),
        retry_backoff_seconds=backoff,
        default_profile=default_profile,
        hermes_home=home,
        profiles={str(key): dict(value) for key, value in profiles.items() if isinstance(value, dict)},
        path=path,
        config_path=Path(fleet_config_path),
    )


def _read_sessions(
    db_path: Path,
    *,
    since: float,
    limit: int,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    if not db_path.is_file():
        return [], None
    try:
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        return [], "sessions_unavailable"
    try:
        conn.row_factory = sqlite3.Row
        columns = {
            str(row["name"]): str(row["type"] or "").upper()
            for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        timestamp_types = {
            columns.get("last_activity_at", ""),
            columns.get("started_at", ""),
        }
        numeric_schema = bool(timestamp_types) and all(
            any(token in declared for token in ("INT", "REAL", "NUM", "FLOAT", "DOUBLE"))
            for declared in timestamp_types
        )
        if numeric_schema:
            rows = conn.execute(
                "SELECT model, model_config, last_activity_at, started_at, ended_at, archived "
                "FROM sessions WHERE ended_at IS NULL AND archived=0 "
                "AND COALESCE(last_activity_at, started_at) >= ? "
                "ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        else:
            timestamp = "COALESCE(last_activity_at, started_at)"
            epoch = (
                f"CASE WHEN typeof({timestamp}) IN ('integer','real') "
                f"OR (typeof({timestamp})='text' AND instr({timestamp}, '-')=0) "
                f"THEN CAST({timestamp} AS REAL) "
                f"ELSE (julianday({timestamp}) - 2440587.5) * 86400.0 END"
            )
            rows = conn.execute(
                "SELECT model, model_config, last_activity_at, started_at, ended_at, archived "
                "FROM sessions WHERE ended_at IS NULL AND archived=0 "
                f"AND {epoch} >= ? ORDER BY {epoch} DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        return [dict(row) for row in rows], None
    except sqlite3.Error:
        return [], "sessions_unavailable"
    finally:
        conn.close()


class SwapFailure(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass
class HermesModelWatcher:
    cfg: HermesGpuConfig
    pin: Optional[PinFn] = None
    manager_factory: Optional[ManagerFactory] = None
    inspect_fn: Optional[InspectFn] = None
    probe_fn: Optional[ProbeFn] = None
    publish_fn: Optional[PublishFn] = None
    clear_fn: Optional[ClearFn] = None
    sleep_fn: SleepFn = asyncio.sleep
    desired: Optional[str] = None
    loaded: Optional[str] = None
    last_working: Optional[str] = None
    phase: str = "idle"
    error: Optional[str] = None
    failure_code: Optional[str] = None
    warning: Optional[str] = None
    attempt: int = 0
    next_attempt_at: Optional[float] = None
    configured_n_ctx: Optional[int] = None
    effective_n_ctx: Optional[int] = None
    context_source: Optional[str] = None
    load_generation: int = 0
    _task: Optional[asyncio.Task] = field(default=None, init=False, repr=False)
    _phase_started: Optional[float] = field(default=None, init=False, repr=False)
    _base_slot_config: Optional[dict[str, Any]] = field(default=None, init=False, repr=False)
    _last_working_config: Optional[dict[str, Any]] = field(default=None, init=False, repr=False)
    _blocked_target: Optional[str] = field(default=None, init=False, repr=False)
    _profile_source: Optional[bytes] = field(default=None, init=False, repr=False)
    _operation_started: Optional[float] = field(default=None, init=False, repr=False)
    _published_profile: Optional[str] = field(default=None, init=False, repr=False)
    _published_context: tuple[Optional[int], Optional[str]] = field(
        default=(None, None), init=False, repr=False
    )
    _session_warning: Optional[str] = field(default=None, init=False, repr=False)
    _profile_warning: Optional[str] = field(default=None, init=False, repr=False)
    _state_warning: Optional[str] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        state = _read_state(self.cfg.state_path)
        last = str(state.get("profile_id") or "")
        if last in self.cfg.profiles:
            self.last_working = last
        self.load_generation = _positive_int(state.get("load_generation")) or 0
        try:
            self._profile_source = self.cfg.path.read_bytes()
        except OSError:
            self._profile_source = None

    @property
    def swapping(self) -> bool:
        return self._task is not None and not self._task.done()

    def _set_phase(self, phase: str, *, code: Optional[str] = None, message: Optional[str] = None) -> None:
        if phase != self.phase:
            now = time.monotonic()
            duration_ms = int((now - self._operation_started) * 1000) if self._operation_started else 0
            self._phase_started = time.monotonic()
            logger.info(
                "Hermes GPU phase=%s desired=%s loaded=%s attempt=%s duration_ms=%s result_code=%s",
                phase,
                self.desired or "none",
                self.loaded or "none",
                self.attempt,
                duration_ms,
                code or "none",
            )
        self.phase = phase
        if code is not None:
            self.failure_code = code
        if message is not None:
            self.error = message

    def snapshot(self) -> dict[str, Any]:
        retry_after = None
        if self.next_attempt_at is not None:
            retry_after = max(0, int(self.next_attempt_at - time.time() + 0.999))
        return {
            "desired": self.desired,
            "loaded": self.loaded,
            "last_working": self.last_working,
            "swapping": self.swapping,
            "phase": self.phase,
            "duration_ms": int((time.monotonic() - self._phase_started) * 1000) if self._phase_started else 0,
            "attempt": self.attempt,
            "max_attempts": self.cfg.max_start_attempts,
            "next_attempt_at": (
                datetime.fromtimestamp(self.next_attempt_at, timezone.utc).isoformat()
                if self.next_attempt_at is not None
                else None
            ),
            "retry_after_seconds": retry_after,
            "error": self.error,
            "failure_code": self.failure_code,
            "warning": self.warning,
            "slot_id": self.cfg.slot_id,
            "configured_n_ctx": self.configured_n_ctx,
            "effective_n_ctx": self.effective_n_ctx,
            "context_source": self.context_source,
            "load_generation": self.load_generation,
            "supports_vision": False,
        }

    def retry_response(self) -> Optional[tuple[str, int]]:
        coordination = self._manager().slot_operation_snapshot(self.cfg.slot_id)
        if coordination.get("manual_stop"):
            return "gpu_slot_stopped", 0
        if self.swapping:
            return "gpu_swap_in_progress", max(1, self.snapshot()["retry_after_seconds"] or 5)
        if self.desired and self.desired != self.loaded:
            if self._blocked_target == self.desired:
                return "gpu_swap_failed", 0
            return "gpu_swap_in_progress", max(
                1, self.snapshot()["retry_after_seconds"] or 5
            )
        return None

    def _manager(self):
        if self.manager_factory is not None:
            return self.manager_factory()
        from local_model_router.helpers.llama_cpp_manager import BackendManager

        return BackendManager.get_instance(str(self.cfg.config_path))

    def prepare_manager(self) -> bool:
        """Give explicit fleet starts a complete profile-backed slot config."""
        manager = self._manager()
        if self.cfg.slot_id not in getattr(manager, "_slot_configs", {}):
            return False
        profile = self.last_working or self.cfg.default_profile
        if profile in self.cfg.profiles:
            manager.set_slot_config(
                self.cfg.slot_id,
                self._profile_config(profile),
            )
        return True

    def reset_retry_budget(self) -> None:
        self._blocked_target = None
        self.failure_code = None
        self.error = None
        self.attempt = 0
        self.next_attempt_at = None
        self._manager().set_auto_restart_suppressed(self.cfg.slot_id, False)

    def _sync_warning(self) -> None:
        self.warning = self._state_warning or self._profile_warning or self._session_warning

    def _refresh_profile_config(self) -> bool:
        """Reload a changed profile file between swaps and reset its retry budget."""
        if self.swapping or not self.cfg.path.is_file():
            return False
        try:
            source = self.cfg.path.read_bytes()
        except OSError:
            self._profile_warning = "profile_reload_failed"
            self._sync_warning()
            return False
        if self._profile_source is None:
            self._profile_source = source
            self._profile_warning = None
            self._sync_warning()
            return False
        if source == self._profile_source:
            self._profile_warning = None
            self._sync_warning()
            return False
        previous_profiles = self.cfg.profiles
        try:
            refreshed = load_hermes_gpu_config(self.cfg.path, self.cfg.config_path)
        except (OSError, yaml.YAMLError):
            refreshed = None
        if refreshed is None or refreshed.slot_id != self.cfg.slot_id:
            self._profile_warning = "profile_reload_failed"
            self._sync_warning()
            logger.warning("Hermes GPU profile reload rejected")
            return False
        profile_keys = {
            key for profile in (*previous_profiles.values(), *refreshed.profiles.values()) for key in profile
        }
        profile_keys.discard("default_max_tokens")
        current = dict(getattr(self._manager(), "_slot_configs", {}).get(self.cfg.slot_id) or {})
        self._base_slot_config = {
            key: value for key, value in current.items() if key not in profile_keys
        }
        for name in (
            "poll_seconds",
            "hermes_home",
            "profiles",
            "active_window_seconds",
            "session_limit",
            "drain_notice_seconds",
            "max_start_attempts",
            "retry_backoff_seconds",
            "default_profile",
        ):
            setattr(self.cfg, name, getattr(refreshed, name))
        self._profile_source = source
        self._profile_warning = None
        self._sync_warning()
        self.reset_retry_budget()
        logger.info("Hermes GPU profiles reloaded result_code=ok")
        return True

    def _slot_port(self) -> int:
        config = getattr(self._manager(), "_slot_configs", {}).get(self.cfg.slot_id) or {}
        return int(config.get("port") or 8080)

    def _profile_config(self, profile_id: str, *, speculative: bool = True) -> dict[str, Any]:
        manager = self._manager()
        if self._base_slot_config is None:
            current = dict(manager._slot_configs[self.cfg.slot_id])
            profile_keys = {key for profile in self.cfg.profiles.values() for key in profile}
            profile_keys.discard("default_max_tokens")
            self._base_slot_config = {key: value for key, value in current.items() if key not in profile_keys}
        profile = {
            key: value
            for key, value in self.cfg.profiles[profile_id].items()
            if key not in {"default_max_tokens", "chat_template_path"}
        }
        template = str(self.cfg.profiles[profile_id].get("chat_template_path") or "").strip()
        args = list(profile.get("extra_args") or [])
        if template and "--chat-template-file" not in args:
            args.extend(["--chat-template-file", template])
        profile["extra_args"] = args
        if not speculative:
            profile["extra_args"] = _without_speculative(list(profile.get("extra_args") or []))
        return {**self._base_slot_config, **profile, "hermes_gpu_profile": profile_id}

    def _validate_profile(self, profile_id: str) -> None:
        profile = self.cfg.profiles[profile_id]
        model_path = Path(str(profile.get("model_path") or ""))
        if not model_path.is_file():
            raise SwapFailure("model_missing", "The selected model file is unavailable")
        template = str(profile.get("chat_template_path") or "").strip()
        args = list(profile.get("extra_args") or [])
        if template and not Path(template).is_file():
            raise SwapFailure("chat_template_missing", "The selected chat template is unavailable")
        if not template and "--chat-template-file" in args:
            index = args.index("--chat-template-file") + 1
            if index >= len(args) or not Path(str(args[index])).is_file():
                raise SwapFailure("chat_template_missing", "The selected chat template is unavailable")

    async def read_loaded(self) -> Optional[str]:
        port = self._slot_port()
        inspected = (
            self.inspect_fn()
            if self.inspect_fn is not None
            else await asyncio.to_thread(_default_inspect)
        )
        process_hint = str(inspected.get(port, ""))
        if not process_hint:
            self._clear_loaded_runtime()
            return None
        probe = self.probe_fn or _default_probe
        result = probe(port)
        if inspect.isawaitable(result):
            result = await result
        if not result.get("healthy"):
            self._clear_loaded_runtime()
            return None
        matched = match_loaded_profile(process_hint, self.cfg.profiles)
        if not matched:
            matched = match_loaded_profile(str(result.get("model") or ""), self.cfg.profiles)
        if not matched:
            self._clear_loaded_runtime()
            return None
        profile = self.cfg.profiles[matched]
        self.configured_n_ctx = _positive_int(profile.get("context_size"))
        self.effective_n_ctx = _positive_int(result.get("effective_n_ctx"))
        self.context_source = str(result.get("context_source") or "") or None
        return matched

    def _read_recent_desired(self) -> Optional[str]:
        rows, error = _read_sessions(
            self.cfg.hermes_home / "state.db",
            since=time.time() - self.cfg.active_window_seconds,
            limit=self.cfg.session_limit,
        )
        self._session_warning = error
        self._sync_warning()
        return pick_session_model(rows)

    def _default_desired(self) -> Optional[str]:
        config_path = self.cfg.hermes_home / "config.yaml"
        if config_path.is_file():
            try:
                mapped = default_model_from_yaml(config_path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                mapped = None
            if mapped in self.cfg.profiles:
                return mapped
        return self.cfg.default_profile if self.cfg.default_profile in self.cfg.profiles else None

    def read_desired(self) -> Optional[str]:
        recent = self._read_recent_desired()
        return recent or self.loaded or self._default_desired()

    async def refresh_desired(self, *, probe_loaded: bool = False) -> str:
        # An ingress refresh is allowed to update the desired session while a
        # swap is running, but it must not publish a half-finished load. The
        # swap task owns loaded-state verification until it releases the slot.
        self._refresh_profile_config()
        if probe_loaded or (self.loaded is None and not self.swapping):
            self.loaded = await self.read_loaded()
            if self.loaded:
                self.last_working = self.loaded
                manager = self._manager()
                current = getattr(manager, "_slot_configs", {}).get(self.cfg.slot_id)
                baseline = self._profile_config(self.loaded)
                self._last_working_config = (
                    {**baseline, **dict(current or {})}
                    if (current or {}).get("hermes_gpu_profile") == self.loaded
                    else baseline
                )
                manager.set_slot_config(self.cfg.slot_id, self._last_working_config)
                self._publish_loaded(self.loaded)
        coordination = self._manager().slot_operation_snapshot(self.cfg.slot_id)
        if coordination.get("manual_stop"):
            self.desired = self.read_desired()
            self.failure_code = None
            self.error = None
            self._set_phase("stopped")
            return "stopped"
        chosen = self.read_desired()
        if chosen != self.desired:
            self.desired = chosen
            self._blocked_target = None
            self.failure_code = None
            self.error = None
            self._manager().set_auto_restart_suppressed(self.cfg.slot_id, False)
        action = decide_swap(self.desired, self.loaded, self.swapping)
        if action == "swap" and self.desired and self._blocked_target != self.desired:
            self._task = asyncio.create_task(self._swap_loop(self.desired))
        elif action == "swap" and self.desired == self._blocked_target:
            action = "blocked"
        elif action == "idle":
            if self.failure_code == "slot_operation_in_progress":
                self.failure_code = None
                self.error = None
            if not self.failure_code:
                self._set_phase("idle")
        return action

    async def tick(self) -> str:
        return await self.refresh_desired(probe_loaded=not self.swapping)

    async def _swap_loop(self, initial_target: str) -> None:
        manager = self._manager()
        if not manager.begin_slot_operation(self.cfg.slot_id, "draining"):
            self.failure_code = None
            self.error = None
            self._set_phase("busy")
            return
        self._operation_started = time.monotonic()
        original = self.loaded or self.last_working
        current_config = dict(
            getattr(manager, "_slot_configs", {}).get(self.cfg.slot_id) or {}
        )
        original_config = dict(self._last_working_config or {})
        if original:
            original_config = {
                **self._profile_config(original),
                **original_config,
                **(current_config if self.loaded else {}),
            }
        target = initial_target
        original_stopped = False
        self.attempt = 0
        self.next_attempt_at = None
        try:
            self._validate_profile(target)
            self._set_phase("draining")
            await manager.wait_for_slot_idle(
                self.cfg.slot_id,
                notice_seconds=self.cfg.drain_notice_seconds,
                on_wait=lambda: self._set_phase("waiting_for_requests"),
            )
            self._set_phase("stopping")
            owns_process = bool(
                getattr(manager, "owns_slot_process", lambda _name: False)(self.cfg.slot_id)
            )
            if self.loaded is not None and not owns_process:
                raise SwapFailure(
                    "port_in_use",
                    "The model port belongs to another or unverifiable process",
                )
            if owns_process:
                if not await manager._stop_slot_unlocked(self.cfg.slot_id):
                    code = getattr(
                        manager, "last_slot_failure_code", lambda _name: ""
                    )(self.cfg.slot_id)
                    raise SwapFailure(
                        code or "slot_stop_failed",
                        "Imperium could not stop the running model process safely",
                    )
                self.loaded = None
                self._clear_loaded_runtime()
                original_stopped = True
            elif not await _wait_port_free(self._slot_port(), timeout=0.5):
                raise SwapFailure(
                    "port_in_use",
                    "The model port belongs to another or unverifiable process",
                )
            if not await _wait_port_free(self._slot_port()):
                raise SwapFailure("port_in_use", "The model port is still occupied")
            while self.attempt < self.cfg.max_start_attempts:
                if self.desired and self.desired != target:
                    target = self.desired
                    self.attempt = 0
                    self._validate_profile(target)
                self.attempt += 1
                speculative = self.attempt == 1
                target_config = self._profile_config(target, speculative=speculative)
                manager.set_slot_config(self.cfg.slot_id, target_config)
                self.configured_n_ctx = _positive_int(
                    self.cfg.profiles[target].get("context_size")
                )
                manager.set_slot_phase(self.cfg.slot_id, "loading")
                self._set_phase("loading")
                result = await manager._start_slot_unlocked(self.cfg.slot_id)
                failure = _start_failure(result)
                if failure is None:
                    observed = await self.read_loaded()
                    if observed != target:
                        failure = SwapFailure(
                            "model_identity_mismatch",
                            "The loaded model identity did not match the selected profile",
                            retryable=True,
                        )
                if failure is None:
                    self.loaded = target
                    self.last_working = target
                    self._last_working_config = dict(target_config)
                    self._blocked_target = None
                    self.failure_code = None
                    self.error = None
                    self.next_attempt_at = None
                    manager.set_auto_restart_suppressed(self.cfg.slot_id, False)
                    self.load_generation += 1
                    self._write_state(target)
                    self._publish_loaded(target)
                    self._set_phase("idle")
                    logger.info(
                        "Hermes GPU ready desired=%s loaded=%s attempts=%s effective_n_ctx=%s duration_ms=%s result_code=ok",
                        self.desired,
                        self.loaded,
                        self.attempt,
                        self.effective_n_ctx or "unknown",
                        int((time.monotonic() - self._operation_started) * 1000),
                    )
                    return

                self.failure_code = failure.code
                self.error = str(failure)
                if getattr(manager, "owns_slot_process", lambda _name: False)(
                    self.cfg.slot_id
                ):
                    if not await manager._stop_slot_unlocked(self.cfg.slot_id):
                        self.failure_code = getattr(
                            manager, "last_slot_failure_code", lambda _name: ""
                        )(self.cfg.slot_id) or "slot_stop_failed"
                        self.error = "Imperium could not clean up the failed model process"
                        break
                if not await _wait_port_free(self._slot_port()):
                    self.failure_code = "port_in_use"
                    self.error = "The model port stayed occupied after a failed start"
                    break
                if not failure.retryable or failure.code in _NON_RETRYABLE_START_CODES:
                    break
                if self.attempt >= self.cfg.max_start_attempts:
                    break
                delay_index = min(self.attempt - 1, len(self.cfg.retry_backoff_seconds) - 1)
                delay = self.cfg.retry_backoff_seconds[delay_index] if self.cfg.retry_backoff_seconds else 0
                self.next_attempt_at = time.time() + delay
                manager.set_slot_phase(self.cfg.slot_id, "backoff")
                self._set_phase("backoff", code=failure.code, message=str(failure))
                await self.sleep_fn(delay)
                self.next_attempt_at = None

            await self._restore_original(manager, original, original_config)
            self._blocked_target = target
            self._set_phase("failed", code=self.failure_code or "gpu_swap_failed", message=self.error)
        except SwapFailure as exc:
            if original_stopped:
                self.failure_code = exc.code
                self.error = str(exc)
                await self._restore_original(manager, original, original_config)
            self._blocked_target = target
            self._set_phase(
                "failed",
                code=self.failure_code or exc.code,
                message=self.error or str(exc),
            )
        except Exception as exc:  # pragma: no cover - defensive guard
            self.failure_code = "gpu_swap_failed"
            self.error = f"GPU model swap failed ({type(exc).__name__})"
            if original_stopped:
                await self._restore_original(manager, original, original_config)
            self._blocked_target = target
            self._set_phase(
                "failed",
                code=self.failure_code,
                message=self.error,
            )
            logger.error("Hermes GPU swap failed (%s)", type(exc).__name__)
        finally:
            self.next_attempt_at = None
            if self._blocked_target is not None and self.phase == "failed":
                manager.set_auto_restart_suppressed(self.cfg.slot_id, True)
            manager.finish_slot_operation(self.cfg.slot_id)
            self._operation_started = None

    async def _restore_original(
        self,
        manager: Any,
        original: Optional[str],
        original_config: dict[str, Any],
    ) -> bool:
        if not original or original not in self.cfg.profiles:
            return False
        self._set_phase("restoring", code=self.failure_code, message=self.error)
        manager.set_slot_phase(self.cfg.slot_id, "restoring")
        try:
            if getattr(manager, "owns_slot_process", lambda _name: False)(self.cfg.slot_id):
                if not await manager._stop_slot_unlocked(self.cfg.slot_id):
                    raise SwapFailure(
                        "restore_failed", "Imperium could not clean up the failed target"
                    )
            if not await _wait_port_free(self._slot_port()):
                raise SwapFailure(
                    "restore_failed", "The model port stayed occupied during recovery"
                )
            restore_config = original_config or self._profile_config(original)
            manager.set_slot_config(self.cfg.slot_id, restore_config)
            restored = await manager._start_slot_unlocked(self.cfg.slot_id)
            if _start_failure(restored) is not None or await self.read_loaded() != original:
                await manager._stop_slot_unlocked(self.cfg.slot_id)
                raise SwapFailure(
                    "restore_failed", "Imperium could not restore the previous model"
                )
            self.loaded = original
            self.last_working = original
            self._last_working_config = dict(restore_config)
            self.load_generation += 1
            self._write_state(original)
            self._publish_loaded(original)
            return True
        except Exception as exc:
            self.loaded = None
            self._clear_loaded_runtime()
            self.failure_code = "restore_failed"
            self.error = "Imperium could not restore the previous model"
            logger.error("Hermes GPU recovery failed (%s)", type(exc).__name__)
            return False

    def _publish_loaded(self, profile_id: str) -> None:
        published_context = (self.effective_n_ctx, self.context_source)
        if self._published_profile == profile_id and self._published_context == published_context:
            return
        model_id = str(self.cfg.profiles[profile_id].get("model_id") or profile_id)
        if self.pin is not None:
            self.pin(model_id)
        if self.publish_fn is not None:
            self.publish_fn(profile_id, model_id, self.effective_n_ctx)
        self._published_profile = profile_id
        self._published_context = published_context

    def _clear_loaded_runtime(self) -> None:
        self.configured_n_ctx = None
        self.effective_n_ctx = None
        self.context_source = None
        if self.clear_fn is not None:
            self.clear_fn(self.cfg.slot_id)
        self._published_profile = None
        self._published_context = (None, None)

    def _write_state(self, profile_id: str) -> None:
        payload = {
            "profile_id": profile_id,
            "load_generation": self.load_generation,
        }
        path = self.cfg.state_path
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, path)
            self._state_warning = None
            self._sync_warning()
        except OSError:
            self._state_warning = "state_write_failed"
            self._sync_warning()
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                self._state_warning = "state_write_failed"
                self._sync_warning()

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                await self.stop()
                raise
            except Exception as exc:
                logger.error("Hermes GPU watcher tick failed (%s)", type(exc).__name__)
            await asyncio.sleep(self.cfg.poll_seconds)

    async def stop(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _without_speculative(args: list[Any]) -> list[Any]:
    cleaned: list[Any] = []
    index = 0
    value_flags = {"--spec-type", "--spec-draft-n-max", "--spec-draft-n-min", "--spec-draft-p-min"}
    while index < len(args):
        flag = str(args[index])
        if flag == "--spec-default":
            index += 1
            continue
        if flag in value_flags:
            index += 2
            continue
        cleaned.append(args[index])
        index += 1
    return cleaned


def _start_failure(result: Any) -> Optional[SwapFailure]:
    if not isinstance(result, dict):
        return SwapFailure("start_failed", "The model process returned an invalid result", retryable=True)
    if result.get("running") and result.get("healthy") and not result.get("error"):
        return None
    code = str(result.get("failure_code") or "start_failed")
    return SwapFailure(code, "The selected model did not become healthy", retryable=code not in _NON_RETRYABLE_START_CODES)


def _positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _read_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Accept the pre-0.12 migration shape once, but normalize it immediately in
    # memory. Every successful write uses only the two recovery fields.
    profile_id = str(data.get("profile_id") or data.get("last_working_profile") or "")
    generation = _positive_int(data.get("load_generation"))
    if not profile_id or generation is None:
        return {}
    return {"profile_id": profile_id, "load_generation": generation}


async def _default_probe(port: int) -> dict[str, Any]:
    result: dict[str, Any] = {"healthy": False}
    timeout = aiohttp.ClientTimeout(total=5)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"http://127.0.0.1:{port}/health") as response:
                data = await response.json(content_type=None)
                result["healthy"] = (
                    response.status == 200
                    and isinstance(data, dict)
                    and data.get("status") == "ok"
                )
            if not result["healthy"]:
                return result
            try:
                async with session.get(f"http://127.0.0.1:{port}/props") as response:
                    props = await response.json(content_type=None)
                    props = props if isinstance(props, dict) else {}
                    settings = props.get("default_generation_settings") or {}
                    settings = settings if isinstance(settings, dict) else {}
                    result["effective_n_ctx"] = _positive_int(settings.get("n_ctx"))
                    if result["effective_n_ctx"]:
                        result["context_source"] = "props"
                    result["model"] = props.get("model_alias")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                pass
            if not result.get("effective_n_ctx") or not result.get("model"):
                try:
                    async with session.get(f"http://127.0.0.1:{port}/v1/models") as response:
                        listing = await response.json(content_type=None)
                        listing = listing if isinstance(listing, dict) else {}
                        row = next((item for item in listing.get("data", []) if isinstance(item, dict)), {})
                        result["model"] = result.get("model") or row.get("id")
                        if not result.get("effective_n_ctx"):
                            result["effective_n_ctx"] = _positive_int((row.get("meta") or {}).get("n_ctx"))
                            if result["effective_n_ctx"]:
                                result["context_source"] = "v1_models"
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    pass
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return result
    return result


async def _wait_port_free(port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                pass
        except OSError:
            return True
        await asyncio.sleep(0.1)
    return False


def _default_inspect() -> dict[int, str]:
    from local_model_router.service.readiness import inspect_running_llama_models

    return inspect_running_llama_models()

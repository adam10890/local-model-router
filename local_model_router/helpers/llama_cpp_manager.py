"""Backend-agnostic llama.cpp slot orchestration and failover."""

from __future__ import annotations

import asyncio
import configparser
import logging
import os
import socket
import threading
import time
from typing import Any, Dict, List, Optional

import aiohttp
import yaml

from local_model_router.helpers.conf_resolver import resolve_conf_path


def _default_config_path() -> str:
    return str(resolve_conf_path())


def _tcp_port_open(host: str, port: Any) -> bool:
    try:
        target = "127.0.0.1" if host in {"", "localhost", "0.0.0.0", "::"} else host
        with socket.create_connection((target, int(port)), timeout=0.2):
            return True
    except (OSError, TypeError, ValueError):
        return False


def resolve_preset_alias(preset_path: str, alias: str, models_dir: str) -> str:
    """Return the model path assigned to *alias* in a llama.cpp preset."""
    if not preset_path or not os.path.exists(preset_path):
        return ""
    preset = configparser.ConfigParser()
    preset.read(preset_path, encoding="utf-8")
    for section in preset.sections():
        if preset.get(section, "alias", fallback=section) == alias:
            model = preset.get(section, "model", fallback="")
            return os.path.normpath(os.path.join(models_dir, model)) if models_dir and model else model
    return ""

class BackendManager:
    """
    High-level manager that uses the new backend abstraction.
    
    Supports:
      - Auto-detection of Docker vs subprocess
      - Parallel slot execution (each slot = independent container/process)
      - Unified status API
    """
    
    _instance: Optional['BackendManager'] = None
    
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = os.path.normcase(os.path.realpath(config_path or _default_config_path()))
        self._backend = None
        self._slot_configs: Dict[str, Dict[str, Any]] = {}
        self.global_config: Dict[str, Any] = {}
        self.logger = logging.getLogger("lmm.backend_manager")
        self._load()
        self._init_failover()  # Initialize failover chains and cooldown probes
    
    @classmethod
    def get_instance(cls, config_path: Optional[str] = None) -> 'BackendManager':
        requested = os.path.normcase(
            os.path.realpath(config_path or _default_config_path())
        )
        if cls._instance is None:
            cls._instance = cls(requested)
        else:
            current = os.path.normcase(os.path.realpath(cls._instance.config_path))
            if requested != current:
                raise ValueError(
                    f"BackendManager already uses a different config path: {current}"
                )
        return cls._instance

    def _ensure_coordination_state(self) -> None:
        """Create process-local slot coordination lazily for legacy test doubles."""
        if not hasattr(self, "_coordination_lock"):
            self._coordination_lock = threading.RLock()
            self._slot_coordination: Dict[str, Dict[str, Any]] = {}

    def _coordination(self, name: str) -> Dict[str, Any]:
        self._ensure_coordination_state()
        with self._coordination_lock:
            return self._slot_coordination.setdefault(name, {
                "phase": "idle",
                "started_at": None,
                "active_requests": 0,
                "epoch": 0,
                "manual_stop": False,
                "restart_suppressed": False,
            })

    def slot_operation_snapshot(self, name: str) -> Dict[str, Any]:
        state = self._coordination(name)
        with self._coordination_lock:
            started = state.get("started_at")
            return {
                **state,
                "duration_ms": int((time.monotonic() - started) * 1000) if started else 0,
            }

    def begin_slot_operation(self, name: str, phase: str) -> bool:
        state = self._coordination(name)
        with self._coordination_lock:
            if state["phase"] != "idle":
                return False
            state.update(phase=phase, started_at=time.monotonic())
            return True

    def set_slot_phase(self, name: str, phase: str) -> None:
        state = self._coordination(name)
        with self._coordination_lock:
            if state["phase"] != "idle":
                state["phase"] = phase

    def finish_slot_operation(self, name: str, *, bump_epoch: bool = True) -> None:
        state = self._coordination(name)
        with self._coordination_lock:
            if bump_epoch:
                state["epoch"] += 1
            state.update(phase="idle", started_at=None)

    def slot_epoch(self, name: str) -> int:
        state = self._coordination(name)
        with self._coordination_lock:
            return int(state["epoch"])

    def slot_accepting_requests(self, name: str) -> bool:
        state = self._coordination(name)
        with self._coordination_lock:
            return state["phase"] == "idle"

    def try_begin_slot_request(self, name: str, *, expected_epoch: Optional[int] = None) -> str:
        """Reserve a slot for one forwarded request; return ok or a retry code."""
        state = self._coordination(name)
        with self._coordination_lock:
            if state["phase"] != "idle":
                return "gpu_swap_in_progress"
            if expected_epoch is not None and state["epoch"] != expected_epoch:
                return "gpu_model_changed"
            state["active_requests"] += 1
            return "ok"

    def finish_slot_request(self, name: str) -> None:
        state = self._coordination(name)
        with self._coordination_lock:
            state["active_requests"] = max(0, int(state["active_requests"]) - 1)

    async def wait_for_slot_idle(
        self,
        name: str,
        *,
        notice_seconds: float = 60.0,
        on_wait: Optional[Any] = None,
    ) -> None:
        """Wait until every accepted request finishes, without cancelling it."""
        warned = False
        started = time.monotonic()
        while self.slot_operation_snapshot(name)["active_requests"]:
            elapsed = time.monotonic() - started
            if not warned and elapsed >= notice_seconds:
                warned = True
                self.set_slot_phase(name, "waiting_for_requests")
                if on_wait is not None:
                    on_wait()
            await asyncio.sleep(0.05)

    def set_slot_config(self, name: str, config: Dict[str, Any]) -> None:
        if name not in self._slot_configs:
            raise KeyError(name)
        self._ensure_coordination_state()
        with self._coordination_lock:
            self._slot_configs[name] = dict(config)

    def configure_process_ownership(self, path: str | os.PathLike[str]) -> None:
        if self._backend and hasattr(self._backend, "configure_process_state"):
            self._backend.configure_process_state(path)

    def adopt_slot(self, name: str) -> bool:
        if not self._backend or not hasattr(self._backend, "adopt_slot"):
            return False
        config = self._slot_configs.get(name)
        return bool(config and self._backend.adopt_slot(name, config))

    def owns_slot_process(self, name: str) -> bool:
        return bool(
            self._backend
            and hasattr(self._backend, "owns_slot")
            and self._backend.owns_slot(name)
        )

    def set_manual_stop(self, name: str, stopped: bool) -> None:
        state = self._coordination(name)
        with self._coordination_lock:
            state["manual_stop"] = bool(stopped)

    def set_auto_restart_suppressed(self, name: str, suppressed: bool) -> None:
        state = self._coordination(name)
        with self._coordination_lock:
            state["restart_suppressed"] = bool(suppressed)
    
    def _load(self) -> None:
        """Load config and create backend."""
        if not os.path.exists(self.config_path):
            self.logger.warning(f"Config not found: {self.config_path}")
            return
        
        with open(self.config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
        
        global_config = config.get('global', {})
        self.global_config = global_config
        
        # Expand env vars
        for key, value in global_config.items():
            if isinstance(value, str) and '${' in value:
                start = value.find('${')
                end = value.find('}', start)
                if end != -1:
                    var = value[start+2:end]
                    global_config[key] = value[:start] + os.environ.get(var, '') + value[end+1:]
        
        # Create backend
        from local_model_router.helpers.backends.factory import create_backend
        self._backend = create_backend(global_config)
        self.logger.info(f"Backend: {self._backend.backend_type.value}")
        
        # Load slot configs
        backend_type = self._backend.backend_type.value if self._backend else str(global_config.get('backend', 'auto')).lower()
        model_cards = {} if backend_type == 'remote' else self._load_model_cards(config)
        models_dir = '' if backend_type == 'remote' else global_config.get('models_dir', '')
        
        for slot in config.get('active_slots', []):
            if not slot or not slot.get('enabled', True):
                continue
            
            name = slot.get('id', f"slot_{slot.get('port', 'unknown')}")
            
            # Resolve model_id → model_path only for backends that load local files.
            model_path = '' if backend_type == 'remote' else slot.get('model_path', '')
            if backend_type != 'remote' and not model_path and slot.get('model_id'):
                model_path = self._resolve_model(slot['model_id'], model_cards, models_dir)
            
            slot_config = dict(slot)
            slot_config['model_path'] = model_path
            self._slot_configs[name] = slot_config

        # Overlay persistent router state (set via dashboard)
        self._apply_router_state()

    def _apply_router_state(self) -> None:
        """Overlay conf/router_state.json onto _slot_configs (non-destructive)."""
        import json  # noqa: PLC0415
        candidate = os.path.join(os.path.dirname(self.config_path), "router_state.json")
        if not os.path.exists(candidate):
            return
        try:
            with open(candidate, encoding="utf-8") as f:
                state = json.load(f)
            for slot_id, overrides in state.items():
                if slot_id in self._slot_configs:
                    self._slot_configs[slot_id].update(overrides)
            self.logger.info(f"Router state loaded from {candidate}")
        except Exception as exc:
            self.logger.warning(f"Could not load router_state.json: {exc}")

    def _load_model_cards(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Load model cards for model_id → path resolution."""
        cards = {}
        conf_dir = os.path.dirname(self.config_path)
        
        for fname in ('model_cards.yaml', 'installed_models.yaml'):
            path = os.path.join(conf_dir, fname)
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        data = yaml.safe_load(f) or {}
                    cards.update(data.get('models', {}))
                except Exception:
                    pass
        return cards
    
    def _resolve_model(self, model_id: str, cards: Dict[str, Any], models_dir: str) -> str:
        """Resolve model_id to file path.
        
        Handles two formats:
          - model_cards.yaml:    file: {path: "subdir/model.gguf"}
          - installed_models.yaml: file: "model.gguf", path: "subdir/"
        """
        card = cards.get(model_id, {})
        if not card:
            return ''
        
        file_info = card.get('file', '')
        if isinstance(file_info, dict):
            # model_cards.yaml nested format
            rel_path = file_info.get('path', '')
        elif isinstance(file_info, str) and file_info:
            # installed_models.yaml flat format: file + path siblings
            dir_path = card.get('path', '')
            rel_path = os.path.join(dir_path, file_info) if dir_path else file_info
        else:
            rel_path = ''
        
        if rel_path and models_dir:
            return os.path.join(models_dir, rel_path)
        return rel_path
    
    @property
    def backend_type(self) -> str:
        return self._backend.backend_type.value if self._backend else "none"
    
    async def _start_slot_unlocked(self, name: str) -> Dict[str, Any]:
        """Start a single slot."""
        if not self._backend:
            return {"error": "No backend initialized"}
        config = self._slot_configs.get(name)
        if not config:
            return {"error": f"Slot '{name}' not found in config"}
        
        status = await self._backend.start_slot(name, config)
        if status.healthy:
            self._restart_attempts.pop(name, None)
        self._start_restart_monitor()
        return {
            "name": status.name,
            "running": status.running,
            "healthy": status.healthy,
            "port": status.port,
            "host": status.host,
            "container_id": status.container_id,
            "pid": status.pid,
            "error": status.error,
            "uptime_s": status.uptime_s,
            "restart_count": status.restart_count,
            "failure_code": status.extra.get("failure_code"),
            "exit_code": status.extra.get("exit_code"),
        }

    async def start_slot(self, name: str) -> Dict[str, Any]:
        if not self.begin_slot_operation(name, "starting"):
            return {
                "name": name,
                "running": False,
                "healthy": False,
                "error": "Another slot operation is already in progress",
                "failure_code": "slot_operation_in_progress",
            }
        try:
            self.set_manual_stop(name, False)
            self.set_auto_restart_suppressed(name, False)
            return await self._start_slot_unlocked(name)
        finally:
            self.finish_slot_operation(name)
    
    async def start_all(self) -> Dict[str, Dict[str, Any]]:
        """Start all configured slots in parallel."""
        if not self._backend:
            return {}

        # Lazy-start cooldown probes now that we have a running event loop.
        if self._cooldown_config.enabled:
            self._start_cooldown_probes()
        
        tasks = []
        names = []
        for name, config in self._slot_configs.items():
            if config.get('auto_load', True):
                tasks.append(self.start_slot(name))
                names.append(name)
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        output = {}
        for name, result in zip(names, results):
            if isinstance(result, Exception):
                output[name] = {"error": str(result)}
            else:
                output[name] = result
        self._start_restart_monitor()
        return output
    
    async def _stop_slot_unlocked(self, name: str) -> bool:
        if not self._backend:
            return False
        stopped = await self._backend.stop_slot(name)
        self._record_slot_failure(name, "")
        if stopped:
            return True
        code = "slot_stop_failed"
        try:
            status = (await self._backend.list_slots()).get(name)
            candidate = str(getattr(status, "extra", {}).get("failure_code") or "")
            if candidate:
                code = candidate
        except Exception:
            pass
        self._record_slot_failure(name, code)
        return False

    def _record_slot_failure(self, name: str, code: str) -> None:
        if not hasattr(self, "_slot_operation_failures"):
            self._slot_operation_failures: Dict[str, str] = {}
        if code:
            self._slot_operation_failures[name] = code
        else:
            self._slot_operation_failures.pop(name, None)

    def last_slot_failure_code(self, name: str) -> str:
        return str(getattr(self, "_slot_operation_failures", {}).get(name) or "")

    async def stop_slot(self, name: str) -> bool:
        if not self.begin_slot_operation(name, "draining"):
            return False
        try:
            await self.wait_for_slot_idle(name)
            self.set_slot_phase(name, "stopping")
            stopped = await self._stop_slot_unlocked(name)
            self.set_manual_stop(name, True)
            return stopped
        finally:
            self.finish_slot_operation(name)
    
    async def stop_all(self) -> Dict[str, bool]:
        restart_task = self._stop_restart_monitor()
        if restart_task is not None:
            await asyncio.gather(restart_task, return_exceptions=True)
        results: Dict[str, bool] = {}
        if not self._backend:
            return results
        for name in self._slot_configs:
            was_owned = self.owns_slot_process(name)
            stopped = await self.stop_slot(name)
            # An untracked, non-running slot is already stopped. A busy slot or
            # a tracked process that refused shutdown remains a real failure.
            busy = self.slot_operation_snapshot(name)["phase"] != "idle"
            config = self._slot_configs.get(name) or {}
            port_busy = bool(
                not stopped
                and config.get("port")
                and _tcp_port_open(str(config.get("host") or "127.0.0.1"), config["port"])
            )
            results[name] = bool(
                stopped or (not was_owned and not busy and not port_busy)
            )
            if not stopped and not busy:
                self.set_manual_stop(name, True)
        return results
    
    async def status(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all slots."""
        if not self._backend:
            return {}
        slots = await self._backend.list_slots()
        names = list(slots)
        checks = await asyncio.gather(
            *(self._backend.health_check(name) for name in names),
            return_exceptions=True,
        )
        for name, check in zip(names, checks):
            if isinstance(check, BaseException):
                slots[name].running = False
                slots[name].healthy = False
                slots[name].error = "Health check failed"
                slots[name].extra["failure_code"] = "health_probe_failed"
            else:
                slots[name] = check
        result = {}
        for name, s in slots.items():
            cfg = self._slot_configs.get(name, {})
            result[name] = {
                "running": s.running,
                "healthy": s.healthy,
                "port": s.port,
                "host": s.host,
                "model_id": s.model_id,
                "container_id": s.container_id,
                "pid": s.pid,
                "error": s.error,
                "uptime_s": s.uptime_s,
                "restart_count": s.restart_count,
                "failure_code": s.extra.get("failure_code"),
                "exit_code": s.extra.get("exit_code"),
                "role": s.extra.get("role", ""),
                # Router Mode fields (from slot config)
                "router_mode": cfg.get("router_mode", False),
                "router_models_dir": cfg.get("router_models_dir", ""),
                "router_models_preset": cfg.get("router_models_preset", ""),
                "router_models_max": cfg.get("router_models_max", 1),
                "router_models_autoload": cfg.get("router_models_autoload", True),
            }
        return result
    
    def get_endpoint(self, role: str) -> Optional[str]:
        """Get the base URL for a slot by role name."""
        if self._backend and hasattr(self._backend, "get_endpoint_by_role"):
            endpoint = self._backend.get_endpoint_by_role(role)
            if endpoint:
                return endpoint
        if self.backend_type == "remote":
            return None
        for name, config in self._slot_configs.items():
            if config.get('role') == role:
                port = config.get('port', 8080)
                return f"http://localhost:{port}/v1"
        return None

    # ═════════════════════════════════════════════════════════════════════════
    # Failover Chain Support (adapted from tiny_router)
    # ═════════════════════════════════════════════════════════════════════════

    def _init_failover(self) -> None:
        """Initialize failover chains and cooldown tracking."""
        from local_model_router.helpers.smart_router.failover import (
            CooldownProbe, CooldownTracker, DEFAULT_CHAINS
        )

        # Load custom chains from config if present
        self._failover_chains = self.global_config.get('failover_chains', DEFAULT_CHAINS)
        self._cooldown_config = CooldownProbe(
            enabled=self.global_config.get('cooldown_probes_enabled', True),
            interval_seconds=self.global_config.get('cooldown_probe_interval', 30),
            max_attempts=self.global_config.get('cooldown_max_attempts', 10),
            probe_timeout=self.global_config.get('cooldown_probe_timeout', 5),
        )
        self._cooldown_tracker = CooldownTracker()
        self._failover_states: Dict[str, Any] = {}  # slot_id -> SlotFailoverState
        self._cooldown_task: Optional[asyncio.Task] = None
        self._restart_task: Optional[asyncio.Task] = None
        self._restart_attempts: Dict[str, int] = {}

        from local_model_router.helpers.smart_router.health import SlotHealthChecker
        self._health_checker = SlotHealthChecker(
            timeout=self.global_config.get('health_check_timeout', 2),
            cache_ttl=self.global_config.get('health_cache_ttl'),
        )

        # Cooldown probes are started lazily in start_all() to avoid
        # calling asyncio.create_task() during synchronous __init__.
        # During agent_init there may be no running event loop yet.

    def _start_restart_monitor(self) -> None:
        """Watch subprocesses started by this manager and restart crashes."""
        if not self.global_config.get("auto_restart") or self.backend_type != "subprocess":
            return
        if self._restart_task is not None and not self._restart_task.done():
            return
        try:
            self._restart_task = asyncio.create_task(self._restart_loop())
            self.logger.info("Subprocess restart monitor started")
        except RuntimeError:
            self.logger.debug("Restart monitor deferred (no event loop)")

    def _stop_restart_monitor(self) -> Optional[asyncio.Task]:
        task = getattr(self, "_restart_task", None)
        if task and not task.done():
            task.cancel()
            self.logger.info("Subprocess restart monitor stopped")
            return task
        return None

    async def _restart_unhealthy_slots(self) -> None:
        max_attempts = max(0, int(self.global_config.get("max_restart_attempts", 3)))
        for name, current in (await self.status()).items():
            if current["healthy"]:
                self._restart_attempts.pop(name, None)
                continue
            if current["running"] or name not in self._slot_configs:
                continue
            coordination = self.slot_operation_snapshot(name)
            if (
                coordination["phase"] != "idle"
                or coordination["manual_stop"]
                or coordination.get("restart_suppressed", False)
            ):
                continue
            attempts = self._restart_attempts.get(name, 0)
            if attempts >= max_attempts:
                continue
            if not self.begin_slot_operation(name, "auto_restarting"):
                continue
            try:
                await self.wait_for_slot_idle(name)
                self._restart_attempts[name] = attempts + 1
                self.logger.warning(
                    "Restarting crashed slot '%s' (%d/%d)",
                    name,
                    attempts + 1,
                    max_attempts,
                )
                await self._backend.start_slot(name, self._slot_configs[name])
            finally:
                self.finish_slot_operation(name)

    async def _restart_loop(self) -> None:
        interval = max(1.0, float(self.global_config.get("health_check_interval", 30)))
        while True:
            try:
                await asyncio.sleep(interval)
                await self._restart_unhealthy_slots()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.error("Subprocess restart monitor error: %s", exc)

    def _start_cooldown_probes(self) -> None:
        """Start background cooldown probe task (requires running event loop)."""
        if self._cooldown_task is not None and not self._cooldown_task.done():
            return
        try:
            self._cooldown_task = asyncio.create_task(self._cooldown_probe_loop())
            self.logger.info("Cooldown probe loop started")
        except RuntimeError:
            # No running event loop — will be retried later from start_all().
            self.logger.debug("Cooldown probes deferred (no event loop)")

    def _stop_cooldown_probes(self) -> None:
        """Stop cooldown probe task."""
        if self._cooldown_task and not self._cooldown_task.done():
            self._cooldown_task.cancel()
            self.logger.info("Cooldown probe loop stopped")

    async def _cooldown_probe_loop(self) -> None:
        """Background loop to probe ERROR slots for recovery."""
        while True:
            try:
                await asyncio.sleep(self._cooldown_config.interval_seconds)

                error_slots = self._cooldown_tracker.get_error_slots()
                for slot_id in error_slots:
                    if not self._cooldown_tracker.should_probe(slot_id, self._cooldown_config):
                        continue

                    self._cooldown_tracker.record_probe(slot_id)
                    config = self._slot_configs.get(slot_id)
                    if not config:
                        continue

                    # Try health check
                    port = config.get('port', 8080)
                    host = config.get('host', 'localhost')
                    url = f"http://{host}:{port}/health"

                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(url, timeout=self._cooldown_config.probe_timeout) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    if data.get('status') == 'ok':
                                        # Slot recovered!
                                        self._cooldown_tracker.mark_recovered(slot_id)
                                        self.logger.info(f"Slot '{slot_id}' recovered via cooldown probe")
                                        # Record in stats
                                        from local_model_router.helpers.stats_tracker import record_failover
                                        record_failover(slot_id, slot_id, "recovery")
                    except Exception:
                        pass  # Still unhealthy, continue probing

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Cooldown probe error: {e}")

    def select_slot_with_failover(
        self,
        role: str,
        preferred_slot: Optional[str] = None,
        chain: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Select a slot for a given role, following failover chain if needed.

        When *chain* is provided (e.g. the ranked-candidate order from
        rank_candidates), it replaces the static role chain so capability
        scoring drives the actual failover order.

        Returns dict with:
            - slot_id: str
            - url: str (full endpoint URL)
            - is_failover: bool
            - failover_reason: str (if is_failover)
        """
        from local_model_router.helpers.smart_router.failover import (
            get_chain_for_role, get_next_in_chain, SlotFailoverState, create_decision
        )

        chain = [str(s) for s in chain if s] if chain else get_chain_for_role(role, self._failover_chains)

        # If preferred_slot specified, start from there; otherwise use first in chain
        start_slot = preferred_slot or (chain[0] if chain else None)
        if not start_slot:
            return None

        # Check if start_slot is healthy
        slot_status = self._get_slot_health(start_slot)

        if slot_status == 'healthy':
            config = self._slot_configs.get(start_slot)
            if config:
                url = self._get_slot_url(start_slot, config)
                return create_decision(
                    slot_id=start_slot,
                    url=url,
                    role=role,
                    reason=f"primary slot for role '{role}'",
                    chain=chain,
                ).__dict__

        # Start_slot unhealthy — walk the failover chain
        current = start_slot
        reason = f"primary slot '{start_slot}' unhealthy" if slot_status == 'unhealthy' else f"primary slot '{start_slot}' not found"

        while current:
            next_slot = get_next_in_chain(current, chain)
            if not next_slot:
                break

            slot_status = self._get_slot_health(next_slot)
            if slot_status == 'healthy':
                config = self._slot_configs.get(next_slot)
                if config:
                    url = self._get_slot_url(next_slot, config)
                    # Record failover in stats
                    from local_model_router.helpers.stats_tracker import record_failover
                    record_failover(start_slot, next_slot, reason)

                    decision = create_decision(
                        slot_id=next_slot,
                        url=url,
                        role=role,
                        reason=f"failover from '{start_slot}' to '{next_slot}'",
                        chain=chain,
                    )
                    decision.is_failover = True
                    decision.failover_reason = reason
                    return decision.__dict__

            current = next_slot
            reason = f"slot '{current}' unhealthy, continuing chain"

        # Chain exhausted — no healthy slot found
        self.logger.warning(f"Failover chain exhausted for role '{role}', no healthy slots")
        return None

    def _get_slot_health(self, slot_id: str) -> str:
        """Check health of a slot: 'healthy', 'unhealthy', or 'unknown'.

        Cooldown check runs in-memory before any network call.
        Network probe is delegated to self._health_checker so it can be
        replaced without touching routing logic (e.g. for async probing
        in Phase 3 or stub injection in tests).
        """
        if slot_id in self._cooldown_tracker.get_error_slots():
            return 'unhealthy'

        if not self._backend:
            return 'unknown'

        config = self._slot_configs.get(slot_id)
        if not config:
            return 'unknown'

        return self._health_checker.check(config)

    async def _get_slot_health_async(self, slot_id: str) -> str:
        """Async version of _get_slot_health. Does not block the event loop."""
        if slot_id in self._cooldown_tracker.get_error_slots():
            return 'unhealthy'

        if not self._backend:
            return 'unknown'

        config = self._slot_configs.get(slot_id)
        if not config:
            return 'unknown'

        return await self._health_checker.check_async(config)

    async def select_slot_with_failover_async(
        self,
        role: str,
        preferred_slot: Optional[str] = None,
        chain: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Async version of select_slot_with_failover. Does not block the event loop.

        Preserves identical chain-walking, cooldown, and preferred_slot logic.
        When *chain* is provided it replaces the static role chain (see the
        sync variant). The sync select_slot_with_failover() still works.
        """
        from local_model_router.helpers.smart_router.failover import (
            get_chain_for_role, get_next_in_chain, create_decision,
        )
        from local_model_router.helpers.stats_tracker import record_failover

        chain = [str(s) for s in chain if s] if chain else get_chain_for_role(role, self._failover_chains)
        start_slot = preferred_slot or (chain[0] if chain else None)
        if not start_slot:
            return None

        slot_status = await self._get_slot_health_async(start_slot)

        if slot_status == 'healthy':
            config = self._slot_configs.get(start_slot)
            if config:
                url = self._get_slot_url(start_slot, config)
                return create_decision(
                    slot_id=start_slot,
                    url=url,
                    role=role,
                    reason=f"primary slot for role '{role}'",
                    chain=chain,
                ).__dict__

        current = start_slot
        reason = (
            f"primary slot '{start_slot}' unhealthy"
            if slot_status == 'unhealthy'
            else f"primary slot '{start_slot}' not found"
        )

        while current:
            next_slot = get_next_in_chain(current, chain)
            if not next_slot:
                break

            slot_status = await self._get_slot_health_async(next_slot)
            if slot_status == 'healthy':
                config = self._slot_configs.get(next_slot)
                if config:
                    url = self._get_slot_url(next_slot, config)
                    record_failover(start_slot, next_slot, reason)
                    decision = create_decision(
                        slot_id=next_slot,
                        url=url,
                        role=role,
                        reason=f"failover from '{start_slot}' to '{next_slot}'",
                        chain=chain,
                    )
                    decision.is_failover = True
                    decision.failover_reason = reason
                    return decision.__dict__

            current = next_slot
            reason = f"slot '{current}' unhealthy, continuing chain"

        self.logger.warning(
            "Async failover chain exhausted for role '%s', no healthy slots", role
        )
        return None

    def _get_slot_url(self, slot_id: str, config: Dict[str, Any]) -> str:
        """Get the full API URL for a slot."""
        port = config.get('port', 8080)
        host = config.get('host', 'localhost')
        return f"http://{host}:{port}/v1"

    def mark_slot_error(self, slot_id: str, error_message: str = "") -> None:
        """Mark a slot as in ERROR state for cooldown probing."""
        self._cooldown_tracker.mark_error(slot_id, error_message)
        self.logger.warning(f"Slot '{slot_id}' marked error: {error_message}")

    def get_failover_status(self) -> Dict[str, Any]:
        """Get current failover and cooldown status for dashboard."""
        from local_model_router.helpers.stats_tracker import get_stats_summary

        stats = get_stats_summary(window="24h")
        return {
            "failover_chains": self._failover_chains,
            "cooldown_enabled": self._cooldown_config.enabled,
            "cooldown_interval": self._cooldown_config.interval_seconds,
            "error_slots_being_probed": self._cooldown_tracker.get_error_slots(),
            "failover_stats": stats.get("failovers", {}),
            "slot_stats": stats.get("slots", []),
        }


# ══════════════════════════════════════════════════════════════════════════════
# Convenience functions (backward compat + new API)

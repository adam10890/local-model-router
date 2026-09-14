"""Hermes GPU selection, verified swaps, retries and recovery."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from starlette.testclient import TestClient

from local_model_router.service.app import create_app
from local_model_router.service.hermes_model_watcher import (
    HermesGpuConfig,
    HermesModelWatcher,
    _default_probe,
    _read_sessions,
    decide_swap,
    default_model_from_yaml,
    load_hermes_gpu_config,
    match_loaded_profile,
    normalize_hermes_model,
    pick_session_model,
    public_hermes_model_ids,
)
from local_model_router.service.observer import ObserverBackend


def test_normalize_hermes_names():
    assert normalize_hermes_model("ornith") == "ornith"
    assert normalize_hermes_model("local") == "ornith"
    assert normalize_hermes_model("uncensored") == "qwen38_27b"
    assert normalize_hermes_model("qwen38_27b") == "qwen38_27b"
    assert normalize_hermes_model("openrouter/foo") is None
    assert normalize_hermes_model("") is None


def test_pick_newest_open_session_and_model_config():
    rows = [
        {"model": "uncensored", "last_activity_at": 1, "ended_at": None, "archived": 0},
        {"model": "ornith", "last_activity_at": 9, "ended_at": None, "archived": 0},
        {"model": "uncensored", "last_activity_at": 99, "ended_at": 10, "archived": 0},
    ]
    assert pick_session_model(rows) == "ornith"
    assert pick_session_model([{
        "model": None,
        "model_config": '{"model": "uncensored"}',
        "last_activity_at": 1,
        "ended_at": None,
        "archived": 0,
    }]) == "qwen38_27b"


def test_default_mapping_and_decision_helpers():
    text = "model:\n  default: ornith\nproviders:\n  lmm-router:\n    default_model: local\n"
    assert default_model_from_yaml(text) == "ornith"
    assert decide_swap("ornith", "ornith", False) == "idle"
    assert decide_swap("qwen38_27b", "ornith", False) == "swap"
    assert decide_swap("qwen38_27b", "ornith", True) == "busy"
    assert decide_swap(None, "ornith", False) == "idle"


def test_match_loaded_from_gguf_stem():
    profiles = {
        "ornith": {"model_id": "ornith", "model_path": r"C:\m\Ornith-1.5-35B-Q4_K_M.gguf"},
        "qwen38_27b": {"model_id": "qwen38_27b", "model_path": r"C:\m\Qwen3.8-27B-OBLITERATED-Q4_K_M.gguf"},
    }
    assert match_loaded_profile("Ornith-1.5-35B-Q4_K_M", profiles) == "ornith"
    assert match_loaded_profile("Qwen3.8-27B-OBLITERATED-Q4_K_M", profiles) == "qwen38_27b"


class FakeManager:
    def __init__(self, cfg: HermesGpuConfig, *, runtime: str | None = "ornith", outcomes=None):
        self.cfg = cfg
        self.runtime = runtime
        self.outcomes = list(outcomes or [])
        self._slot_configs = {cfg.slot_id: {"model_id": "ornith", "port": 18888}}
        self.phase = "idle"
        self.epoch = 0
        self.stops: list[str] = []
        self.starts: list[dict] = []
        self.waited = 0
        self.restart_suppressed = False

    def begin_slot_operation(self, _name, phase):
        if self.phase != "idle":
            return False
        self.phase = phase
        return True

    def slot_operation_snapshot(self, _name):
        return {
            "phase": self.phase,
            "manual_stop": False,
            "epoch": self.epoch,
            "active_requests": 0,
        }

    def finish_slot_operation(self, _name, **_kwargs):
        self.phase = "idle"
        self.epoch += 1

    def set_slot_phase(self, _name, phase):
        self.phase = phase

    def set_auto_restart_suppressed(self, _name, suppressed):
        self.restart_suppressed = bool(suppressed)

    async def wait_for_slot_idle(self, _name, **_kwargs):
        self.waited += 1

    def set_slot_config(self, name, config):
        self._slot_configs[name] = dict(config)

    def owns_slot_process(self, _name):
        return self.runtime is not None

    async def _stop_slot_unlocked(self, name):
        self.stops.append(name)
        had_runtime = self.runtime is not None
        self.runtime = None
        return had_runtime

    async def _start_slot_unlocked(self, name):
        config = dict(self._slot_configs[name])
        self.starts.append(config)
        result = self.outcomes.pop(0) if self.outcomes else {"running": True, "healthy": True}
        if result.get("running") and result.get("healthy") and not result.get("error"):
            self.runtime = config["hermes_gpu_profile"]
        return result


def _cfg(tmp_path: Path, *, backoffs=(15.0, 60.0)) -> HermesGpuConfig:
    home = tmp_path / "hermes"
    home.mkdir(exist_ok=True)
    profiles = {}
    for profile_id, model_id in (("ornith", "ornith"), ("qwen38_27b", "qwen38_27b")):
        model = tmp_path / f"{profile_id}.gguf"
        template = tmp_path / f"{profile_id}.jinja"
        model.touch()
        template.touch()
        args = []
        if profile_id == "ornith":
            args += ["--spec-default", "--spec-type", "draft-mtp", "--spec-draft-n-max", "2"]
        profiles[profile_id] = {
            "model_id": model_id,
            "model_path": str(model),
            "chat_template_path": str(template),
            "context_size": 131072,
            "default_max_tokens": 16384 if profile_id == "ornith" else 4096,
            "supports_vision": False,
            "extra_args": args,
        }
    return HermesGpuConfig(
        slot_id="slot_chat",
        poll_seconds=3.0,
        hermes_home=home,
        profiles=profiles,
        path=tmp_path / "hermes_gpu_profiles.yaml",
        config_path=tmp_path / "llama_cpp_servers.yaml",
        retry_backoff_seconds=backoffs,
    )


def _watcher(tmp_path, *, runtime="ornith", outcomes=None, sleeps=None, backoffs=(15.0, 60.0)):
    cfg = _cfg(tmp_path, backoffs=backoffs)
    manager = FakeManager(cfg, runtime=runtime, outcomes=outcomes)

    def inspect_models():
        if manager.runtime is None:
            return {}
        return {18888: cfg.profiles[manager.runtime]["model_path"]}

    async def probe(_port):
        if manager.runtime is None:
            return {"healthy": False}
        return {
            "healthy": True,
            "model": cfg.profiles[manager.runtime]["model_id"],
            "effective_n_ctx": 65536,
            "context_source": "props",
        }

    async def sleep(delay):
        if sleeps is not None:
            sleeps.append(delay)

    watcher = HermesModelWatcher(
        cfg=cfg,
        manager_factory=lambda: manager,
        inspect_fn=inspect_models,
        probe_fn=probe,
        sleep_fn=sleep,
    )
    return watcher, manager


async def _free_port(*_args, **_kwargs):
    return True


def test_tick_is_noop_when_verified_model_is_loaded(tmp_path):
    watcher, manager = _watcher(tmp_path)
    watcher.read_desired = lambda: "ornith"  # type: ignore[method-assign]
    assert asyncio.run(watcher.tick()) == "idle"
    assert manager.stops == []
    assert watcher.snapshot()["effective_n_ctx"] == 65536


def test_swap_stop_start_pin_and_atomic_state(tmp_path, monkeypatch):
    monkeypatch.setattr("local_model_router.service.hermes_model_watcher._wait_port_free", _free_port)
    pins: list[str] = []
    watcher, manager = _watcher(tmp_path)
    watcher.pin = pins.append
    watcher.read_desired = lambda: "qwen38_27b"  # type: ignore[method-assign]

    async def run():
        assert await watcher.tick() == "swap"
        await watcher._task

    asyncio.run(run())
    assert manager.stops == ["slot_chat"]
    assert [row["hermes_gpu_profile"] for row in manager.starts] == ["qwen38_27b"]
    assert pins[-1] == "qwen38_27b"
    assert watcher.loaded == "qwen38_27b"
    state = json.loads(watcher.cfg.state_path.read_text(encoding="utf-8"))
    assert set(state) == {"profile_id", "load_generation"}
    assert state["profile_id"] == "qwen38_27b"
    assert state["load_generation"] == 1


def test_success_log_has_desired_loaded_duration_and_result_code(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(
        "local_model_router.service.hermes_model_watcher._wait_port_free", _free_port
    )
    watcher, _manager = _watcher(tmp_path)
    watcher.read_desired = lambda: "qwen38_27b"  # type: ignore[method-assign]

    async def run():
        await watcher.tick()
        await watcher._task

    with caplog.at_level(
        logging.INFO, logger="uvicorn.error.imperium.hermes_gpu"
    ):
        asyncio.run(run())

    messages = "\n".join(caplog.messages)
    assert "phase=loading desired=qwen38_27b" in messages
    assert "loaded=qwen38_27b" in messages
    assert "duration_ms=" in messages
    assert "result_code=ok" in messages


def test_failed_mtp_attempt_retries_without_speculative_flags(tmp_path, monkeypatch):
    monkeypatch.setattr("local_model_router.service.hermes_model_watcher._wait_port_free", _free_port)
    sleeps: list[float] = []
    failed = {"running": False, "healthy": False, "failure_code": "process_exited", "error": "failed"}
    watcher, manager = _watcher(tmp_path, runtime="qwen38_27b", outcomes=[failed, {"running": True, "healthy": True}], sleeps=sleeps)
    watcher.read_desired = lambda: "ornith"  # type: ignore[method-assign]

    async def run():
        await watcher.tick()
        await watcher._task

    asyncio.run(run())
    assert "--spec-type" in manager.starts[0]["extra_args"]
    assert "--chat-template-file" in manager.starts[0]["extra_args"]
    assert "--spec-type" not in manager.starts[1]["extra_args"]
    assert "--chat-template-file" in manager.starts[1]["extra_args"]
    assert sleeps == [15.0]
    assert watcher.loaded == "ornith"


def test_restore_reuses_last_working_non_speculative_config(tmp_path, monkeypatch):
    monkeypatch.setattr("local_model_router.service.hermes_model_watcher._wait_port_free", _free_port)
    failed = {
        "running": False,
        "healthy": False,
        "failure_code": "process_exited",
        "error": "failed",
    }
    watcher, manager = _watcher(
        tmp_path,
        outcomes=[failed, failed, failed, {"running": True, "healthy": True}],
    )
    manager._slot_configs["slot_chat"] = watcher._profile_config(
        "ornith", speculative=False
    )
    watcher.read_desired = lambda: "qwen38_27b"  # type: ignore[method-assign]

    async def run():
        await watcher.tick()
        await watcher._task

    asyncio.run(run())
    assert manager.starts[-1]["hermes_gpu_profile"] == "ornith"
    assert "--spec-type" not in manager.starts[-1]["extra_args"]


def test_target_change_during_backoff_restarts_serially_with_fresh_budget(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("local_model_router.service.hermes_model_watcher._wait_port_free", _free_port)
    failed = {
        "running": False,
        "healthy": False,
        "failure_code": "process_exited",
        "error": "failed",
    }
    watcher, manager = _watcher(
        tmp_path,
        runtime="qwen38_27b",
        outcomes=[failed, {"running": True, "healthy": True}],
    )
    watcher.read_desired = lambda: "ornith"  # type: ignore[method-assign]

    async def change_target(_delay):
        watcher.desired = "qwen38_27b"

    watcher.sleep_fn = change_target

    async def run():
        await watcher.tick()
        await watcher._task

    asyncio.run(run())
    assert [row["hermes_gpu_profile"] for row in manager.starts] == [
        "ornith",
        "qwen38_27b",
    ]
    assert watcher.loaded == "qwen38_27b"
    assert watcher.attempt == 1


def test_three_failures_restore_once_and_block_same_target(tmp_path, monkeypatch):
    monkeypatch.setattr("local_model_router.service.hermes_model_watcher._wait_port_free", _free_port)
    sleeps: list[float] = []
    failed = {"running": False, "healthy": False, "failure_code": "process_exited", "error": "failed"}
    watcher, manager = _watcher(
        tmp_path,
        outcomes=[failed, failed, failed, {"running": True, "healthy": True}],
        sleeps=sleeps,
    )
    watcher.read_desired = lambda: "qwen38_27b"  # type: ignore[method-assign]

    async def run():
        await watcher.tick()
        await watcher._task

    asyncio.run(run())
    assert [row["hermes_gpu_profile"] for row in manager.starts] == [
        "qwen38_27b", "qwen38_27b", "qwen38_27b", "ornith"
    ]
    assert sleeps == [15.0, 60.0]
    assert watcher.loaded == "ornith"
    assert watcher.phase == "failed"
    assert watcher.retry_response() == ("gpu_swap_failed", 0)
    assert asyncio.run(watcher.tick()) == "blocked"
    assert len(manager.starts) == 4


def test_missing_model_fails_before_drain_or_stop(tmp_path):
    watcher, manager = _watcher(tmp_path)
    Path(watcher.cfg.profiles["qwen38_27b"]["model_path"]).unlink()
    watcher.read_desired = lambda: "qwen38_27b"  # type: ignore[method-assign]

    async def run():
        await watcher.tick()
        await watcher._task

    asyncio.run(run())
    assert watcher.failure_code == "model_missing"
    assert manager.waited == 0
    assert manager.stops == []


def test_stop_failure_blocks_swap_without_starting_target(tmp_path):
    watcher, manager = _watcher(tmp_path)
    watcher.read_desired = lambda: "qwen38_27b"  # type: ignore[method-assign]

    async def refuse_stop(_name):
        return False

    manager._stop_slot_unlocked = refuse_stop
    manager.last_slot_failure_code = lambda _name: "process_ownership_unverified"

    async def run():
        await watcher.tick()
        await watcher._task

    asyncio.run(run())
    assert manager.starts == []
    assert watcher.loaded == "ornith"
    assert watcher.phase == "failed"
    assert watcher.failure_code == "process_ownership_unverified"


def test_stop_success_with_stuck_port_clears_loaded_and_fails_recovery(
    tmp_path, monkeypatch
):
    async def occupied(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        "local_model_router.service.hermes_model_watcher._wait_port_free", occupied
    )
    watcher, manager = _watcher(tmp_path)
    watcher.read_desired = lambda: "qwen38_27b"  # type: ignore[method-assign]

    async def run():
        await watcher.tick()
        await watcher._task

    asyncio.run(run())
    assert manager.stops == ["slot_chat"]
    assert manager.starts == []
    assert watcher.loaded is None
    assert watcher.failure_code == "restore_failed"
    assert manager.restart_suppressed is True


def test_unverified_occupied_port_blocks_empty_slot_start(tmp_path, monkeypatch):
    async def occupied(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        "local_model_router.service.hermes_model_watcher._wait_port_free", occupied
    )
    watcher, manager = _watcher(tmp_path, runtime=None)
    watcher.read_desired = lambda: "ornith"  # type: ignore[method-assign]

    async def run():
        await watcher.tick()
        await watcher._task

    asyncio.run(run())
    assert manager.starts == []
    assert watcher.loaded is None
    assert watcher.failure_code == "port_in_use"


def test_verified_foreign_model_process_is_not_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "local_model_router.service.hermes_model_watcher._wait_port_free", _free_port
    )
    watcher, manager = _watcher(tmp_path)
    watcher.read_desired = lambda: "qwen38_27b"  # type: ignore[method-assign]
    manager.owns_slot_process = lambda _name: False

    async def run():
        await watcher.tick()
        await watcher._task

    asyncio.run(run())

    assert manager.stops == []
    assert manager.starts == []
    assert watcher.failure_code == "port_in_use"
    assert watcher.loaded == "ornith"


def test_failed_restore_leaves_slot_empty_and_requires_operator(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "local_model_router.service.hermes_model_watcher._wait_port_free", _free_port
    )
    failed = {
        "running": False,
        "healthy": False,
        "failure_code": "process_exited",
        "error": "failed",
    }
    watcher, manager = _watcher(
        tmp_path,
        outcomes=[failed, failed, failed, failed],
    )
    watcher.read_desired = lambda: "qwen38_27b"  # type: ignore[method-assign]

    async def run():
        await watcher.tick()
        await watcher._task

    asyncio.run(run())
    assert [row["hermes_gpu_profile"] for row in manager.starts][-1] == "ornith"
    assert watcher.loaded is None
    assert watcher.phase == "failed"
    assert watcher.failure_code == "restore_failed"
    assert watcher.retry_response() == ("gpu_swap_failed", 0)


def test_start_exception_cleans_up_restores_once_and_suppresses_restart(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "local_model_router.service.hermes_model_watcher._wait_port_free", _free_port
    )
    watcher, manager = _watcher(
        tmp_path,
        outcomes=[RuntimeError("boom"), {"running": True, "healthy": True}],
    )
    watcher.read_desired = lambda: "qwen38_27b"  # type: ignore[method-assign]

    async def run():
        await watcher.tick()
        await watcher._task

    asyncio.run(run())
    assert [row["hermes_gpu_profile"] for row in manager.starts] == [
        "qwen38_27b",
        "ornith",
    ]
    assert watcher.loaded == "ornith"
    assert watcher.phase == "failed"
    assert watcher.failure_code == "gpu_swap_failed"
    assert manager.restart_suppressed is True


def test_model_identity_mismatch_never_publishes_target_and_restores_original(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "local_model_router.service.hermes_model_watcher._wait_port_free", _free_port
    )
    watcher, manager = _watcher(tmp_path, backoffs=())

    def unknown_process():
        return {18888: "unknown-model.gguf"} if manager.runtime else {}

    async def always_ornith(_port):
        return {
            "healthy": manager.runtime is not None,
            "model": "ornith",
            "effective_n_ctx": 65536,
            "context_source": "props",
        }

    watcher.inspect_fn = unknown_process
    watcher.probe_fn = always_ornith
    watcher.read_desired = lambda: "qwen38_27b"  # type: ignore[method-assign]

    async def run():
        await watcher.tick()
        await watcher._task

    asyncio.run(run())
    assert watcher.loaded == "ornith"
    assert watcher.phase == "failed"
    assert watcher.failure_code == "model_identity_mismatch"
    assert [row["hermes_gpu_profile"] for row in manager.starts][-1] == "ornith"


def test_successful_load_survives_optional_selection_state_write_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "local_model_router.service.hermes_model_watcher._wait_port_free", _free_port
    )
    watcher, _manager = _watcher(tmp_path)
    watcher.read_desired = lambda: "qwen38_27b"  # type: ignore[method-assign]
    monkeypatch.setattr(
        "local_model_router.service.hermes_model_watcher.os.replace",
        lambda *_args: (_ for _ in ()).throw(OSError("disk full")),
    )

    async def run():
        await watcher.tick()
        await watcher._task

    asyncio.run(run())
    assert watcher.loaded == "qwen38_27b"
    assert watcher.phase == "idle"
    assert watcher.warning == "state_write_failed"
    assert watcher._read_recent_desired() is None
    assert watcher.warning == "state_write_failed"


def test_no_loaded_model_starts_default_without_trying_to_stop(tmp_path, monkeypatch):
    monkeypatch.setattr("local_model_router.service.hermes_model_watcher._wait_port_free", _free_port)
    watcher, manager = _watcher(tmp_path, runtime=None)
    async def run():
        await watcher.tick()
        await watcher._task

    asyncio.run(run())
    assert manager.stops == []
    assert watcher.loaded == "ornith"


def test_ingress_refresh_during_swap_does_not_probe_or_publish_loaded(tmp_path):
    watcher, _manager = _watcher(tmp_path, runtime=None)

    def unexpected_probe():
        raise AssertionError("an ingress refresh must not inspect a half-finished load")

    watcher.inspect_fn = unexpected_probe

    async def run():
        blocker = asyncio.create_task(asyncio.Event().wait())
        watcher._task = blocker
        try:
            assert await watcher.refresh_desired(probe_loaded=False) == "busy"
            assert watcher.desired == "ornith"
            assert watcher.loaded is None
        finally:
            blocker.cancel()
            await asyncio.gather(blocker, return_exceptions=True)

    asyncio.run(run())


def test_stale_state_is_not_evidence_of_loaded_model(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.state_path.write_text(
        json.dumps({"profile_id": "qwen38_27b", "load_generation": 7}),
        encoding="utf-8",
    )
    manager = FakeManager(cfg, runtime=None)
    watcher = HermesModelWatcher(
        cfg=cfg,
        manager_factory=lambda: manager,
        inspect_fn=lambda: {},
        probe_fn=lambda _port: {"healthy": False},
    )
    assert watcher.last_working == "qwen38_27b"
    assert watcher.loaded is None
    assert asyncio.run(watcher.read_loaded()) is None


def test_stale_state_cannot_override_verified_live_profile(tmp_path):
    cfg = _cfg(tmp_path)
    # A pre-0.12 state file remains valid recovery input after the one-time
    # migration, but live process and model identity remain authoritative.
    cfg.state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "last_working_profile": "qwen38_27b",
                "load_generation": 7,
            }
        ),
        encoding="utf-8",
    )
    manager = FakeManager(cfg, runtime="ornith")

    async def probe(_port):
        return {
            "healthy": True,
            "model": "ornith",
            "effective_n_ctx": 65536,
            "context_source": "props",
        }

    watcher = HermesModelWatcher(
        cfg=cfg,
        manager_factory=lambda: manager,
        inspect_fn=lambda: {18888: cfg.profiles["ornith"]["model_path"]},
        probe_fn=probe,
    )
    watcher.prepare_manager()
    assert manager._slot_configs["slot_chat"]["hermes_gpu_profile"] == "qwen38_27b"

    assert asyncio.run(watcher.tick()) == "idle"

    assert watcher.loaded == "ornith"
    assert manager._slot_configs["slot_chat"]["hermes_gpu_profile"] == "ornith"
    assert manager._slot_configs["slot_chat"]["model_path"] == cfg.profiles["ornith"]["model_path"]


def test_changed_profile_file_reloads_once_and_resets_failed_target(tmp_path):
    cfg = _cfg(tmp_path)
    payload = {
        "slot_id": cfg.slot_id,
        "default_profile": cfg.default_profile,
        "hermes_home": str(cfg.hermes_home),
        "profiles": json.loads(json.dumps(cfg.profiles)),
    }
    cfg.path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    manager = FakeManager(cfg, runtime="ornith")
    watcher = HermesModelWatcher(cfg=cfg, manager_factory=lambda: manager)
    watcher.loaded = "ornith"
    watcher.desired = "ornith"
    watcher.read_desired = lambda: "ornith"  # type: ignore[method-assign]
    watcher._blocked_target = "ornith"
    watcher.failure_code = "process_exited"
    watcher.error = "failed"
    watcher.attempt = 3
    manager.restart_suppressed = True

    payload["profiles"]["ornith"]["default_max_tokens"] = 8192
    cfg.path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    assert asyncio.run(watcher.refresh_desired()) == "idle"

    assert watcher.cfg.profiles["ornith"]["default_max_tokens"] == 8192
    assert watcher._blocked_target is None
    assert watcher.failure_code is None
    assert watcher.error is None
    assert watcher.attempt == 0
    assert manager.restart_suppressed is False

    watcher._blocked_target = "ornith"
    watcher.failure_code = "process_exited"
    watcher.attempt = 3
    assert asyncio.run(watcher.refresh_desired()) == "idle"
    assert watcher._blocked_target == "ornith"
    assert watcher.failure_code == "process_exited"
    assert watcher.attempt == 3


def test_busy_swap_retries_after_shared_operation_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "local_model_router.service.hermes_model_watcher._wait_port_free", _free_port
    )
    watcher, manager = _watcher(tmp_path)
    watcher.read_desired = lambda: "qwen38_27b"  # type: ignore[method-assign]
    manager.phase = "loading"

    async def run():
        assert await watcher.tick() == "swap"
        await watcher._task

    asyncio.run(run())
    assert watcher.phase == "busy"
    assert watcher.failure_code is None
    assert watcher.retry_response() == ("gpu_swap_in_progress", 5)

    manager.phase = "idle"

    async def resume():
        assert await watcher.tick() == "swap"
        await watcher._task

    asyncio.run(resume())
    assert watcher.phase == "idle"
    assert watcher.failure_code is None
    assert watcher.loaded == "qwen38_27b"


def test_recent_session_query_filters_and_limits(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (model TEXT, model_config TEXT, last_activity_at REAL, "
        "started_at REAL, ended_at REAL, archived INTEGER)"
    )
    now = time.time()
    rows = [("ornith", None, now - i, now - i, None, 0) for i in range(40)]
    rows += [("uncensored", None, now + 10, now + 10, 1, 0), ("uncensored", None, now + 20, now + 20, None, 1)]
    conn.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()

    selected, warning = _read_sessions(db, since=now - 900, limit=32)
    assert warning is None
    assert len(selected) == 32
    assert all(row["ended_at"] is None and row["archived"] == 0 for row in selected)


def test_recent_session_query_ignores_stale_rows_and_uses_started_at_fallback(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (model TEXT, model_config TEXT, last_activity_at REAL, "
        "started_at REAL, ended_at REAL, archived INTEGER)"
    )
    now = time.time()
    conn.executemany(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("uncensored", None, now - 901, now - 901, None, 0),
            ("ornith", None, None, now - 30, None, 0),
        ],
    )
    conn.commit()
    conn.close()

    selected, warning = _read_sessions(db, since=now - 900, limit=32)

    assert warning is None
    assert [row["model"] for row in selected] == ["ornith"]
    assert pick_session_model(selected) == "ornith"


def test_recent_session_query_and_picker_accept_iso_timestamps(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (model TEXT, model_config TEXT, last_activity_at TEXT, "
        "started_at TEXT, ended_at TEXT, archived INTEGER)"
    )
    now = datetime.now(timezone.utc)
    conn.executemany(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                "uncensored",
                None,
                (now - timedelta(minutes=16)).isoformat(),
                (now - timedelta(minutes=16)).isoformat(),
                None,
                0,
            ),
            (
                "ornith",
                None,
                (now - timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
                (now - timedelta(minutes=3)).isoformat(),
                None,
                0,
            ),
            (
                "uncensored",
                None,
                (now - timedelta(minutes=1)).isoformat(),
                (now - timedelta(minutes=2)).isoformat(),
                now.isoformat(),
                0,
            ),
        ],
    )
    conn.commit()
    conn.close()

    selected, warning = _read_sessions(
        db, since=(now - timedelta(minutes=15)).timestamp(), limit=32
    )

    assert warning is None
    assert [row["model"] for row in selected] == ["ornith"]
    assert pick_session_model(selected) == "ornith"


def test_profile_reload_warning_survives_successful_session_read(tmp_path):
    cfg = _cfg(tmp_path)
    payload = {
        "slot_id": cfg.slot_id,
        "default_profile": cfg.default_profile,
        "hermes_home": str(cfg.hermes_home),
        "profiles": cfg.profiles,
    }
    cfg.path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    manager = FakeManager(cfg)
    watcher = HermesModelWatcher(cfg=cfg, manager_factory=lambda: manager)
    cfg.path.write_text("profiles: []\n", encoding="utf-8")

    assert watcher._refresh_profile_config() is False
    assert watcher.warning == "profile_reload_failed"
    assert watcher._read_recent_desired() is None
    assert watcher.warning == "profile_reload_failed"

    payload["profiles"]["ornith"]["default_max_tokens"] = 8192
    cfg.path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    assert watcher._refresh_profile_config() is True
    assert watcher.warning is None


def test_no_recent_session_keeps_verified_loaded_model(tmp_path):
    watcher, manager = _watcher(tmp_path, runtime="ornith")
    db = watcher.cfg.hermes_home / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (model TEXT, model_config TEXT, last_activity_at REAL, "
        "started_at REAL, ended_at REAL, archived INTEGER)"
    )
    stale = time.time() - watcher.cfg.active_window_seconds - 1
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        ("uncensored", None, stale, stale, None, 0),
    )
    conn.commit()
    conn.close()

    assert asyncio.run(watcher.tick()) == "idle"
    assert watcher.desired == "ornith"
    assert watcher.loaded == "ornith"
    assert manager.starts == []
    assert manager.stops == []


def test_profile_config_invalid_numeric_policy_uses_bounded_defaults(tmp_path):
    path = tmp_path / "hermes_gpu_profiles.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "session_limit": "invalid",
                "max_start_attempts": None,
                "retry_backoff_seconds": [15, "invalid"],
                "profiles": {"ornith": {"model_id": "ornith"}},
            }
        ),
        encoding="utf-8",
    )

    cfg = load_hermes_gpu_config(path, tmp_path / "fleet.yaml")

    assert cfg is not None
    assert cfg.session_limit == 32
    assert cfg.max_start_attempts == 3
    assert cfg.retry_backoff_seconds == (15.0, 60.0)


def test_sqlite_failure_warns_and_keeps_loaded_model(tmp_path, monkeypatch):
    watcher, _manager = _watcher(tmp_path)
    watcher.loaded = "ornith"
    (watcher.cfg.hermes_home / "state.db").touch()
    monkeypatch.setattr(
        "local_model_router.service.hermes_model_watcher.sqlite3.connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )
    assert watcher.read_desired() == "ornith"
    assert watcher.warning == "sessions_unavailable"


def test_observer_separates_configured_and_measured_context(tmp_path):
    cfg = tmp_path / "llama_cpp_servers.yaml"
    cfg.write_text(
        "active_slots:\n  - id: slot_chat\n    port: 8080\n    role: chat\n    enabled: true\n    model_id: ornith\n    context_size: 131072\n",
        encoding="utf-8",
    )
    observer = ObserverBackend(str(cfg))
    observer.set_runtime_slot(
        "slot_chat",
        model_id="ornith",
        configured_n_ctx=131072,
        effective_n_ctx=65536,
        context_source="props",
    )
    slot = observer.get_slots()[0]
    assert slot["configured_context_size"] == 131072
    assert slot["effective_context_size"] == 65536
    assert slot["context_size"] == 65536
    assert slot["context_source"] == "props"


def test_observer_does_not_publish_configured_context_as_measured(tmp_path):
    cfg = tmp_path / "llama_cpp_servers.yaml"
    cfg.write_text(
        "active_slots:\n  - id: slot_chat\n    port: 8080\n    role: chat\n    enabled: true\n",
        encoding="utf-8",
    )
    observer = ObserverBackend(str(cfg))
    observer.set_runtime_slot(
        "slot_chat",
        model_id="ornith",
        configured_n_ctx=131072,
        effective_n_ctx=None,
        context_source=None,
    )

    slot = observer.get_slots()[0]

    assert slot["configured_context_size"] == 131072
    assert slot["effective_context_size"] is None
    assert slot["context_size"] is None


def test_observer_clears_unverified_runtime_overlay(tmp_path):
    cfg = tmp_path / "llama_cpp_servers.yaml"
    cfg.write_text(
        "active_slots:\n  - id: slot_chat\n    port: 8080\n    role: chat\n    enabled: true\n    model_id: ornith\n    context_size: 131072\n",
        encoding="utf-8",
    )
    observer = ObserverBackend(str(cfg))
    observer.set_runtime_slot(
        "slot_chat",
        model_id="qwen38_27b",
        configured_n_ctx=131072,
        effective_n_ctx=65536,
        context_source="props",
    )

    observer.clear_runtime_slot("slot_chat")
    slot = observer.get_slots()[0]

    assert slot["model_id"] == "ornith"
    assert slot["effective_context_size"] is None
    assert slot["context_source"] is None


def test_unknown_live_identity_clears_published_runtime_facts(tmp_path):
    cleared = []
    watcher, _manager = _watcher(tmp_path)
    watcher.clear_fn = cleared.append
    watcher._published_profile = "ornith"
    watcher.configured_n_ctx = 131072
    watcher.effective_n_ctx = 65536
    watcher.context_source = "props"
    watcher.inspect_fn = lambda: {18888: "unknown-model.gguf"}
    watcher.probe_fn = lambda _port: {
        "healthy": True,
        "model": "unknown-model",
        "effective_n_ctx": 4096,
        "context_source": "props",
    }

    assert asyncio.run(watcher.read_loaded()) is None
    assert cleared == ["slot_chat"]
    assert watcher.configured_n_ctx is None
    assert watcher.effective_n_ctx is None
    assert watcher.context_source is None


def test_v1_models_lists_gpu_aliases_and_unknown_live_context(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    monkeypatch.setenv("A0_LMM_ROUTER_HERMES_WATCHER", "0")
    fleet = tmp_path / "llama_cpp_servers.yaml"
    fleet.write_text("active_slots: []\nglobal:\n  backend: remote\n", encoding="utf-8")
    (tmp_path / "hermes_gpu_profiles.yaml").write_text(
        yaml.safe_dump({
            "slot_id": "slot_chat",
            "profiles": {
                "ornith": {
                    "model_id": "ornith",
                    "model_path": "ornith.gguf",
                    "context_size": 131072,
                    "supports_tools": False,
                    "supports_json_mode": True,
                },
                "qwen38_27b": {"model_id": "qwen38_27b", "model_path": "qwen.gguf", "context_size": 131072},
            },
        }),
        encoding="utf-8",
    )
    app = create_app(str(fleet), harnesses_path=str(tmp_path / "missing-harnesses.yaml"))
    rows = TestClient(app).get("/v1/models").json()["data"]
    by_id = {row["id"]: row for row in rows}
    assert set(public_hermes_model_ids()) <= set(by_id)
    assert by_id["ornith"]["context_size"] is None
    assert by_id["ornith"]["meta"]["configured_n_ctx"] == 131072
    assert by_id["ornith"]["meta"]["effective_n_ctx"] is None
    assert by_id["ornith"]["capabilities"]["vision"] is False
    assert by_id["ornith"]["capabilities"]["tools"] is False
    assert by_id["ornith"]["capabilities"]["json_mode"] is True


def test_watcher_activation_requires_subprocess_fleet_control_and_honors_disable(
    tmp_path, monkeypatch, caplog
):
    from local_model_router.helpers.llama_cpp_manager import BackendManager

    model = tmp_path / "ornith.gguf"
    model.touch()
    fleet = tmp_path / "llama_cpp_servers.yaml"
    fleet.write_text(
        "active_slots:\n"
        "  - id: slot_chat\n"
        "    port: 18888\n"
        "    role: chat\n"
        "    enabled: true\n"
        "global:\n"
        "  backend: subprocess\n"
        "  auto_restart: false\n",
        encoding="utf-8",
    )
    (tmp_path / "hermes_gpu_profiles.yaml").write_text(
        yaml.safe_dump(
            {
                "slot_id": "slot_chat",
                "hermes_home": str(tmp_path / "hermes"),
                "profiles": {
                    "ornith": {"model_id": "ornith", "model_path": str(model)}
                },
            }
        ),
        encoding="utf-8",
    )
    kwargs = {
        "harnesses_path": str(tmp_path / "missing-harnesses.yaml"),
        "setup_home": str(tmp_path / "imperium"),
    }

    monkeypatch.delenv("A0_LMM_ROUTER_ENABLE_FLEET_CONTROL", raising=False)
    monkeypatch.delenv("A0_LMM_ROUTER_HERMES_WATCHER", raising=False)
    BackendManager._instance = None
    assert create_app(str(fleet), **kwargs).state.hermes_watcher is None

    monkeypatch.setenv("A0_LMM_ROUTER_ENABLE_FLEET_CONTROL", "1")
    BackendManager._instance = None
    assert create_app(str(fleet), **kwargs).state.hermes_watcher is not None

    fleet.write_text(
        fleet.read_text(encoding="utf-8").replace("enabled: true", "enabled: false"),
        encoding="utf-8",
    )
    BackendManager._instance = None
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="local_model_router.service.app"):
        app = create_app(str(fleet), **kwargs)
    assert app.state.hermes_watcher is None
    assert "Hermes GPU watcher disabled (managed slot unavailable)" in caplog.messages

    fleet.write_text(
        fleet.read_text(encoding="utf-8").replace("enabled: false", "enabled: true"),
        encoding="utf-8",
    )
    monkeypatch.setenv("A0_LMM_ROUTER_HERMES_WATCHER", "0")
    BackendManager._instance = None
    assert create_app(str(fleet), **kwargs).state.hermes_watcher is None
    BackendManager._instance = None


def test_default_probe_reads_props_and_falls_back_to_v1_models(monkeypatch):
    import aiohttp

    response_sets = [
        {
            "/health": {"status": "ok"},
            "/props": {
                "model_alias": "ornith",
                "default_generation_settings": {"n_ctx": 65536},
            },
        },
        {
            "/health": {"status": "ok"},
            "/props": {},
            "/v1/models": {
                "data": [{"id": "qwen38_27b", "meta": {"n_ctx": 32768}}]
            },
        },
    ]
    calls: list[str] = []

    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def json(self, content_type=None):
            return self.payload

    class Session:
        def __init__(self, *_args, **_kwargs):
            self.responses = response_sets.pop(0)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, url):
            path = "/v1/models" if url.endswith("/v1/models") else "/" + url.rsplit("/", 1)[-1]
            calls.append(path)
            return Response(self.responses[path])

    monkeypatch.setattr(aiohttp, "ClientSession", Session)

    props = asyncio.run(_default_probe(18888))
    fallback = asyncio.run(_default_probe(18888))

    assert props == {
        "healthy": True,
        "model": "ornith",
        "effective_n_ctx": 65536,
        "context_source": "props",
    }
    assert fallback == {
        "healthy": True,
        "model": "qwen38_27b",
        "effective_n_ctx": 32768,
        "context_source": "v1_models",
    }
    assert calls == ["/health", "/props", "/health", "/props", "/v1/models"]


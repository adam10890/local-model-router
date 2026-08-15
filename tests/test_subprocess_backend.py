from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from local_model_router.helpers.backends.base import SlotStatus
from local_model_router.helpers.backends.subprocess_backend import SubprocessBackend


class _Process:
    pid = 1234

    def __init__(self, exit_code=None):
        self.exit_code = exit_code

    def poll(self):
        return self.exit_code


def test_start_records_process_exit_and_restart(tmp_path, monkeypatch):
    model = tmp_path / "model.gguf"
    model.touch()
    backend = SubprocessBackend({"log_dir": str(tmp_path / "logs")})
    processes = [_Process(17), _Process()]
    monkeypatch.setattr(backend, "_build_command", lambda _config: ["llama-server"])
    monkeypatch.setattr(
        "local_model_router.helpers.backends.subprocess_backend.subprocess.Popen",
        lambda *_args, **_kwargs: processes.pop(0),
    )

    async def first_fails(_port, _timeout, _proc):
        return False

    monkeypatch.setattr(backend, "_wait_healthy", first_fails)
    config = {"model_path": str(model), "model_id": "local", "port": 8080}

    failed = asyncio.run(backend.start_slot("chat", config))

    assert failed.extra == {"failure_code": "process_exited", "exit_code": 17}
    assert failed.error == "Process exited during startup (exit code 17)"

    async def second_succeeds(_port, _timeout, _proc):
        return True

    monkeypatch.setattr(backend, "_wait_healthy", second_succeeds)
    restarted = asyncio.run(backend.start_slot("chat", config))

    assert restarted.healthy is True
    assert restarted.restart_count == 1


def test_health_recovery_clears_stale_failure(monkeypatch):
    backend = SubprocessBackend({})
    backend._processes["chat"] = _Process()
    backend._started_at["chat"] = time.monotonic() - 2
    backend._slots["chat"] = SlotStatus(
        name="chat",
        port=8080,
        error="Process not running",
        extra={"failure_code": "process_exited", "exit_code": 17},
    )

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def json(self):
            return {"status": "ok"}

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(aiohttp, "ClientSession", Session)

    recovered = asyncio.run(backend.health_check("chat"))

    assert recovered.running is True
    assert recovered.healthy is True
    assert recovered.error is None
    assert recovered.extra == {}
    assert recovered.uptime_s >= 2


def test_build_command_includes_mmproj(tmp_path):
    model = tmp_path / "model.gguf"
    mmproj = tmp_path / "mmproj.gguf"
    model.touch()
    mmproj.touch()
    backend = SubprocessBackend({})
    cmd = backend._build_command({
        "model_path": str(model),
        "model_id": "ornith",
        "mmproj_path": str(mmproj),
        "port": 8081,
        "context_size": 2048,
        "batch_size": 64,
        "threads": 2,
        "parallel_slots": 1,
        "gpu_layers": 0,
        "fit": False,
    })
    assert "--mmproj" in cmd
    assert str(mmproj) in cmd


def test_manager_status_exposes_safe_runtime_fields():
    from local_model_router.helpers.llama_cpp_manager import BackendManager

    class Backend:
        async def list_slots(self):
            return {
                "chat": SlotStatus(
                    name="chat",
                    running=True,
                    healthy=True,
                    restart_count=2,
                    uptime_s=4.5,
                )
            }

        async def health_check(self, name):
            assert name == "chat"
            return SlotStatus(
                name="chat",
                running=False,
                healthy=False,
                restart_count=2,
                uptime_s=4.5,
                extra={"failure_code": "process_exited", "exit_code": 9},
            )

    manager = object.__new__(BackendManager)
    manager._backend = Backend()
    manager._slot_configs = {"chat": {}}

    status = asyncio.run(manager.status())["chat"]

    assert status["failure_code"] == "process_exited"
    assert status["exit_code"] == 9
    assert status["restart_count"] == 2
    assert status["uptime_s"] == 4.5


def test_manager_restarts_crashed_subprocess_up_to_limit():
    from local_model_router.helpers.llama_cpp_manager import BackendManager

    class Backend:
        def __init__(self):
            self.starts = 0

        async def list_slots(self):
            return {"chat": SlotStatus(name="chat", running=False, healthy=False)}

        async def health_check(self, _name):
            return SlotStatus(name="chat", running=False, healthy=False)

        async def start_slot(self, name, config):
            assert name == "chat"
            assert config == {"model_path": "model.gguf"}
            self.starts += 1
            return SlotStatus(name=name, running=True, healthy=True)

    backend = Backend()
    manager = object.__new__(BackendManager)
    manager._backend = backend
    manager._slot_configs = {"chat": {"model_path": "model.gguf"}}
    manager.global_config = {"max_restart_attempts": 2}
    manager._restart_attempts = {}
    manager.logger = logging.getLogger("test.restart")

    asyncio.run(manager._restart_unhealthy_slots())
    asyncio.run(manager._restart_unhealthy_slots())
    asyncio.run(manager._restart_unhealthy_slots())

    assert backend.starts == 2
    assert manager._restart_attempts == {"chat": 2}

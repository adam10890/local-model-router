from __future__ import annotations

import asyncio
import logging
import subprocess
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


def test_build_command_preserves_supported_flags(tmp_path):
    model = tmp_path / "model.gguf"
    mmproj = tmp_path / "mmproj.gguf"
    model.touch()
    mmproj.touch()
    backend = SubprocessBackend({"server_binary": "llama-server"})

    cmd = backend._build_command({
        "model_path": str(model),
        "model_id": "safe-alias",
        "mmproj_path": str(mmproj),
        "port": 8088,
        "host": "127.0.0.1",
        "context_size": 4096,
        "batch_size": 128,
        "threads": 6,
        "parallel_slots": 2,
        "gpu_layers": 12,
        "flash_attention": True,
        "fit": True,
        "fit_target_mib": 768,
        "embedding_mode": True,
        "reasoning_format": "deepseek",
        "jinja": False,
        "extra_args": ["--no-warmup"],
    })

    expected_pairs = {
        "-m": str(model), "--alias": "safe-alias", "--host": "127.0.0.1",
        "--port": "8088", "-c": "4096", "-b": "128", "-t": "6",
        "-np": "2", "-ngl": "12", "--flash-attn": "on",
        "--fit-target": "768", "--reasoning-format": "deepseek",
        "--mmproj": str(mmproj),
    }
    for flag, value in expected_pairs.items():
        assert cmd[cmd.index(flag) + 1] == value
    for flag in ("--fit", "--embedding", "--no-jinja", "--no-warmup"):
        assert flag in cmd


def test_start_rejects_unsafe_slot_name_without_writing_logs(tmp_path):
    model = tmp_path / "model.gguf"
    model.touch()
    log_dir = tmp_path / "logs"
    backend = SubprocessBackend({"log_dir": str(log_dir)})

    status = asyncio.run(backend.start_slot("../outside", {"model_path": str(model)}))

    assert status.extra["failure_code"] == "invalid_slot_name"
    assert not log_dir.exists()


def test_start_failure_and_missing_model_do_not_expose_paths(tmp_path, monkeypatch):
    backend = SubprocessBackend({"log_dir": str(tmp_path / "logs")})
    missing = asyncio.run(backend.start_slot("chat", {"model_path": str(tmp_path / "secret.gguf")}))
    assert missing.error == "Model file not found"
    assert str(tmp_path) not in missing.error

    model = tmp_path / "model.gguf"
    model.touch()
    monkeypatch.setattr(backend, "_build_command", lambda _config: ["llama-server"])
    monkeypatch.setattr(
        "local_model_router.helpers.backends.subprocess_backend.subprocess.Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(str(tmp_path / "secret"))),
    )
    failed = asyncio.run(backend.start_slot("chat", {"model_path": str(model)}))

    assert failed.error == "Could not start local model process"
    assert failed.extra == {"failure_code": "start_failed", "exception_type": "OSError"}
    assert str(tmp_path) not in failed.error


def test_start_timeout_tracks_process_and_closes_parent_log_handle(tmp_path, monkeypatch):
    model = tmp_path / "model.gguf"
    model.touch()
    backend = SubprocessBackend({"log_dir": str(tmp_path / "logs"), "startup_timeout": 0})
    process = _Process()
    captured = {}
    monkeypatch.setattr(backend, "_build_command", lambda _config: ["llama-server"])
    monkeypatch.setattr(backend, "_process_create_time", lambda _pid: 12.5)

    def popen(*_args, **kwargs):
        captured["stdout"] = kwargs["stdout"]
        return process

    monkeypatch.setattr(
        "local_model_router.helpers.backends.subprocess_backend.subprocess.Popen", popen
    )

    status = asyncio.run(backend.start_slot("chat", {"model_path": str(model), "port": 8080}))

    assert status.running is True
    assert status.healthy is False
    assert status.extra["failure_code"] == "health_timeout"
    assert backend._process_identities == {"chat": 12.5}
    assert captured["stdout"].closed is True


def test_wsl_start_tracks_foreground_process_without_shell_backgrounding(tmp_path, monkeypatch):
    model = tmp_path / "model with space.gguf"
    model.touch()
    backend = SubprocessBackend({"log_dir": str(tmp_path / "logs"), "use_wsl": True})
    captured = {}
    monkeypatch.setattr(
        "local_model_router.helpers.backends.subprocess_backend.sys.platform", "win32"
    )
    monkeypatch.setattr(backend, "_wait_healthy", lambda *_args: _async_value(True))
    monkeypatch.setattr(backend, "_process_create_time", lambda _pid: 1.0)

    def popen(cmd, **_kwargs):
        captured["cmd"] = cmd
        return _Process()

    monkeypatch.setattr(
        "local_model_router.helpers.backends.subprocess_backend.subprocess.Popen", popen
    )

    status = asyncio.run(backend.start_slot("chat", {"model_path": str(model)}))

    assert status.healthy is True
    assert captured["cmd"][:3] == ["wsl", "bash", "-lc"]
    assert "nohup" not in captured["cmd"][3]
    assert "&" not in captured["cmd"][3]
    assert "'/mnt/" in captured["cmd"][3]


async def _async_value(value):
    return value


def test_stop_refuses_recycled_pid_and_keeps_tracking(monkeypatch):
    class Process(_Process):
        def send_signal(self, _signal):
            raise AssertionError("recycled process must not be signalled")

    backend = SubprocessBackend({})
    backend._processes["chat"] = Process()
    backend._slots["chat"] = SlotStatus(name="chat", pid=1234)
    backend._process_identities["chat"] = 1.0
    monkeypatch.setattr(backend, "_process_create_time", lambda _pid: 2.0)

    assert asyncio.run(backend.stop_slot("chat")) is False
    assert "chat" in backend._processes
    assert backend._slots["chat"].extra["failure_code"] == "process_identity_changed"


def test_stop_kills_after_timeout_and_cleanup_removes_only_tracked_processes(monkeypatch):
    class Process(_Process):
        def __init__(self):
            super().__init__()
            self.terminated = 0
            self.killed = 0

        def terminate(self):
            self.terminated += 1

        def wait(self, timeout):
            if not self.killed:
                raise subprocess.TimeoutExpired("llama-server", timeout)

        def kill(self):
            self.killed += 1

    process = Process()
    backend = SubprocessBackend({})
    backend._processes["chat"] = process
    backend._slots["chat"] = SlotStatus(name="chat")
    monkeypatch.setattr(backend, "_process_create_time", lambda _pid: None)
    monkeypatch.setattr(
        "local_model_router.helpers.backends.subprocess_backend.sys.platform", "linux"
    )

    asyncio.run(backend.cleanup())

    assert process.terminated == 1
    assert process.killed == 1
    assert asyncio.run(backend.list_slots()) == {}
    assert asyncio.run(backend.stop_slot("unknown")) is False


def test_health_check_maps_unhealthy_and_unavailable_server(monkeypatch):
    backend = SubprocessBackend({})
    backend._processes["chat"] = _Process()
    backend._slots["chat"] = SlotStatus(name="chat", port=8080)

    class Response:
        status = 503

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    unhealthy = asyncio.run(backend.health_check("chat"))
    assert unhealthy.error == "Health check failed"
    assert unhealthy.extra["failure_code"] == "health_probe_failed"

    monkeypatch.setattr(
        aiohttp, "ClientSession", lambda: (_ for _ in ()).throw(OSError("private path"))
    )
    unavailable = asyncio.run(backend.health_check("chat"))
    assert unavailable.error == "Health check failed"
    assert "private path" not in unavailable.error

    unknown = asyncio.run(backend.health_check("missing"))
    assert unknown.error == "Unknown slot"


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

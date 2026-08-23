from __future__ import annotations

import asyncio
import builtins
from types import SimpleNamespace

import aiohttp
from starlette.testclient import TestClient

from local_model_router.helpers.backends.base import SlotStatus
from local_model_router.helpers.backends import docker_backend as docker_module
from local_model_router.helpers.backends.docker_backend import DockerBackend


class _NotFound(Exception):
    pass


class _DeviceRequest:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _Container:
    def __init__(
        self,
        *,
        name="a0-lmm-chat",
        status="running",
        labels=None,
        container_id="0123456789abcdef",
    ):
        self.name = name
        self.status = status
        self.labels = labels or {}
        self.id = container_id
        self.attrs = {"Created": "2026-08-20T00:00:00Z"}
        self.reloads = 0
        self.stop_calls = []
        self.remove_calls = []

    def reload(self):
        self.reloads += 1

    def stop(self, **kwargs):
        self.stop_calls.append(kwargs)
        self.status = "exited"

    def remove(self, **kwargs):
        self.remove_calls.append(kwargs)


class _Networks:
    def __init__(self, missing=False):
        self.missing = missing
        self.get_calls = []
        self.create_calls = []

    def get(self, name):
        self.get_calls.append(name)
        if self.missing:
            raise _NotFound(name)
        return object()

    def create(self, name, **kwargs):
        self.create_calls.append((name, kwargs))


class _Containers:
    def __init__(self):
        self.run_result = None
        self.run_calls = []
        self.listed = []
        self.list_calls = []

    def run(self, image, **kwargs):
        self.run_calls.append({"image": image, **kwargs})
        if isinstance(self.run_result, BaseException):
            raise self.run_result
        return self.run_result or _Container(name=kwargs["name"], labels=kwargs["labels"])

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return list(self.listed)


class _Client:
    def __init__(self, *, missing_network=False):
        self.networks = _Networks(missing_network)
        self.containers = _Containers()


class _Docker:
    types = SimpleNamespace(DeviceRequest=_DeviceRequest)

    def __init__(self, client):
        self.client = client
        self.from_env_calls = 0

    def from_env(self):
        self.from_env_calls += 1
        return self.client


def _install_fake_docker(monkeypatch, *, missing_network=False):
    client = _Client(missing_network=missing_network)
    docker = _Docker(client)
    monkeypatch.setattr(docker_module, "_docker", docker)
    monkeypatch.setattr(
        docker_module,
        "_docker_errors",
        SimpleNamespace(NotFound=_NotFound),
    )
    return docker, client


def _fleet_config(tmp_path, backend="docker"):
    config = tmp_path / "llama_cpp_servers.yaml"
    config.write_text(
        """\
active_slots:
  - id: chat
    port: 8080
    role: chat
    enabled: true
    model_id: chat-model
global:
  backend: %s
""" % backend,
        encoding="utf-8",
    )
    return config


def _service_client(tmp_path, monkeypatch, *, control):
    from local_model_router.helpers.compute_monitor import CPUStats, ComputeSnapshot
    from local_model_router.helpers.llama_cpp_manager import BackendManager
    from local_model_router.service.app import create_app

    BackendManager._instance = None
    if control:
        monkeypatch.setenv("A0_LMM_ROUTER_ENABLE_FLEET_CONTROL", "1")
    else:
        monkeypatch.delenv("A0_LMM_ROUTER_ENABLE_FLEET_CONTROL", raising=False)

    async def health_probe(_url, _timeout):
        return {"ok": True}

    monkeypatch.setattr(
        "local_model_router.helpers.smart_router.health._aiohttp_probe",
        health_probe,
    )
    monkeypatch.setattr(
        "local_model_router.service.app.scan_hardware",
        lambda: ComputeSnapshot(
            1_750_000_000.0,
            (),
            CPUStats(10.0, 32768, 8192, 24576),
        ),
    )
    return TestClient(create_app(str(_fleet_config(tmp_path)))), BackendManager


def test_missing_docker_extra_raises_import_error(monkeypatch):
    real_import = builtins.__import__

    def import_without_docker(name, *args, **kwargs):
        if name == "docker" or name.startswith("docker."):
            raise ImportError("docker blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(docker_module, "_docker", None)
    monkeypatch.setattr(docker_module, "_docker_errors", None)
    monkeypatch.setattr(builtins, "__import__", import_without_docker)

    try:
        docker_module._ensure_docker()
        assert False, "missing docker extra must fail"
    except ImportError as exc:
        assert "Docker SDK not installed" in str(exc)


def test_missing_docker_extra_maps_all_control_actions_to_503(tmp_path, monkeypatch):
    monkeypatch.setattr(
        docker_module,
        "_ensure_docker",
        lambda: (_ for _ in ()).throw(ImportError("Docker SDK not installed")),
    )
    client, manager_cls = _service_client(tmp_path, monkeypatch, control=True)

    for path in (
        "/fleet/slots/chat/start",
        "/fleet/slots/chat/stop",
        "/fleet/start",
        "/fleet/stop",
    ):
        response = client.post(path)
        assert response.status_code == 503, path
        assert response.json()["error"]["code"] == "backend_dependency_missing"

    manager_cls._instance = None


def test_start_preserves_flags_mount_environment_labels_and_stop_scope(tmp_path, monkeypatch):
    docker, client = _install_fake_docker(monkeypatch, missing_network=True)
    models_dir = tmp_path / "models"
    backend = DockerBackend(
        {
            "models_dir": str(models_dir),
            "cuda_visible_devices": "0",
            "api_key": "must-not-enter-container",
            "startup_timeout": 1,
        }
    )

    async def healthy(_port, _timeout):
        return True

    monkeypatch.setattr(backend, "_wait_healthy", healthy)
    status = asyncio.run(
        backend.start_slot(
            "chat",
            {
                "model_path": str(models_dir / "chat.gguf"),
                "model_id": "chat-model",
                "port": 8081,
                "context_size": 16384,
                "batch_size": 256,
                "threads": 8,
                "parallel_slots": 2,
                "gpu_layers": 12,
                "flash_attention": True,
                "fit": True,
                "fit_target_mib": 2048,
                "jinja": False,
            },
        )
    )

    assert docker.from_env_calls == 1
    assert client.networks.create_calls == [("a0-lmm-net", {"driver": "bridge"})]
    call = client.containers.run_calls[0]
    assert call["name"] == "a0-lmm-chat"
    assert call["ports"] == {"8081/tcp": 8081}
    assert call["volumes"] == {
        str(models_dir): {"bind": "/models", "mode": "ro"}
    }
    assert call["environment"] == {"CUDA_VISIBLE_DEVICES": "0"}
    assert call["labels"] == {"a0.lmm.slot": "chat", "a0.lmm.managed": "true"}
    assert call["restart_policy"] == {"Name": "unless-stopped"}
    assert call["device_requests"][0].kwargs == {
        "count": -1,
        "capabilities": [["gpu"]],
    }
    assert call["command"][:2] == ["--model", "/models/chat.gguf"]
    for flag in (
        "--ctx-size",
        "--batch-size",
        "--threads",
        "--parallel",
        "--n-gpu-layers",
        "--flash-attn",
        "--fit-target",
        "--no-jinja",
    ):
        assert flag in call["command"]
    assert status.running is True
    assert status.healthy is True
    assert status.container_id == "0123456789ab"

    container = backend._containers["chat"]
    assert asyncio.run(backend.stop_slot("other")) is False
    assert container.stop_calls == []
    assert asyncio.run(backend.stop_slot("chat")) is True
    assert container.stop_calls == [{"timeout": 10}]
    assert container.remove_calls == [{"force": True}]
    assert asyncio.run(backend.stop_slot("chat")) is False


def test_stale_container_is_replaced_and_timeout_is_reported(tmp_path, monkeypatch):
    _docker, client = _install_fake_docker(monkeypatch)
    backend = DockerBackend({"models_dir": str(tmp_path), "startup_timeout": 1})
    stale = _Container(status="exited")
    replacement = _Container()
    backend._containers["chat"] = stale
    backend._slots["chat"] = SlotStatus(name="chat", port=8080)
    client.containers.run_result = replacement

    async def unhealthy(_port, _timeout):
        return False

    monkeypatch.setattr(backend, "_wait_healthy", unhealthy)
    status = asyncio.run(
        backend.start_slot(
            "chat",
            {"model_path": str(tmp_path / "chat.gguf"), "gpu_layers": 0},
        )
    )

    assert stale.remove_calls == [{"force": True}]
    assert client.containers.run_calls[0]["device_requests"] is None
    assert status.running is False
    assert status.healthy is False
    assert status.error == "Container started but health check failed"


def test_health_list_and_stop_only_touch_identified_managed_slots(tmp_path, monkeypatch):
    _docker, client = _install_fake_docker(monkeypatch)
    backend = DockerBackend({"models_dir": str(tmp_path)})
    managed = _Container(
        name="a0-lmm-managed",
        labels={"a0.lmm.slot": "managed", "a0.lmm.managed": "true"},
    )
    unrelated = _Container(name="unrelated", labels={})
    client.containers.listed = [managed, unrelated]

    slots = asyncio.run(backend.list_slots())

    assert set(slots) == {"managed"}
    assert client.containers.list_calls == [
        {"all": True, "filters": {"label": "a0.lmm.managed=true"}}
    ]
    slots["managed"].port = 8090

    calls = []

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

        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    health = asyncio.run(backend.health_check("managed"))

    assert health.running is True
    assert health.healthy is True
    assert calls == [("http://localhost:8090/health", {"timeout": 5})]
    assert asyncio.run(backend.stop_slot("unrelated")) is False
    assert unrelated.stop_calls == []
    assert asyncio.run(backend.stop_slot("managed")) is True
    assert managed.stop_calls == [{"timeout": 10}]


def test_docker_socket_is_never_touched_when_fleet_control_is_disabled(tmp_path, monkeypatch):
    touches = []

    def forbidden():
        touches.append(True)
        raise AssertionError("Docker socket path was touched")

    monkeypatch.setattr(docker_module, "_ensure_docker", forbidden)
    client, manager_cls = _service_client(tmp_path, monkeypatch, control=False)

    status = client.get("/fleet/status")
    assert status.status_code == 200
    assert status.json()["docker_socket_enabled"] is False
    assert status.json()["fleet_control"] == {
        "enabled": False,
        "backend": "docker",
        "supports_start_stop": True,
    }
    for path in (
        "/fleet/slots/chat/start",
        "/fleet/slots/chat/stop",
        "/fleet/start",
        "/fleet/stop",
    ):
        response = client.post(path)
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "fleet_control_disabled"

    assert touches == []
    assert manager_cls._instance is None

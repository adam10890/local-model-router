"""Tests for the opt-in fleet lifecycle control endpoints.

Hermetic: no Docker daemon, no llama.cpp processes. BackendManager
lifecycle methods are stubbed; only the gating, auth, and payload
mapping are exercised for real.
"""
from __future__ import annotations

from starlette.testclient import TestClient


_ROUTING_CONFIG = """\
active_slots:
  - id: chat
    port: 8080
    host: localhost
    role: chat
    enabled: true
    model_id: chat-model
    context_size: 65536
  - id: utility
    port: 8088
    host: localhost
    role: utility
    enabled: true
    model_id: utility-model
    context_size: 32768
global:
  backend: remote
"""


def _make_app(tmp_path, monkeypatch, *, control=False, api_key=None):
    from local_model_router.helpers.llama_cpp_manager import BackendManager
    from local_model_router.service.app import create_app

    BackendManager._instance = None
    cfg = tmp_path / "llama_cpp_servers.yaml"
    cfg.write_text(_ROUTING_CONFIG, encoding="utf-8")

    async def health_probe(url, timeout):
        return {"ok": True}

    monkeypatch.setattr(
        "local_model_router.helpers.smart_router.health._aiohttp_probe",
        health_probe,
    )

    if control:
        monkeypatch.setenv("A0_LMM_ROUTER_ENABLE_FLEET_CONTROL", "1")
    else:
        monkeypatch.delenv("A0_LMM_ROUTER_ENABLE_FLEET_CONTROL", raising=False)
    if api_key is not None:
        monkeypatch.setenv("A0_LMM_ROUTER_API_KEY", api_key)
    else:
        monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)

    return TestClient(create_app(str(cfg))), BackendManager


def test_fleet_control_disabled_by_default(tmp_path, monkeypatch):
    client, manager_cls = _make_app(tmp_path, monkeypatch, control=False)

    for path in ("/fleet/slots/chat/start", "/fleet/slots/chat/stop", "/fleet/start", "/fleet/stop"):
        resp = client.post(path)
        assert resp.status_code == 403, path
        assert resp.json()["error"]["code"] == "fleet_control_disabled"
    manager_cls._instance = None


def test_fleet_status_reports_control_block(tmp_path, monkeypatch):
    client, manager_cls = _make_app(tmp_path, monkeypatch, control=False)
    body = client.get("/fleet/status").json()
    assert body["fleet_control"] == {"enabled": False, "backend": "remote"}
    assert body["docker_socket_enabled"] is False
    manager_cls._instance = None

    client, manager_cls = _make_app(tmp_path, monkeypatch, control=True)
    body = client.get("/fleet/status").json()
    assert body["fleet_control"] == {"enabled": True, "backend": "remote"}
    # remote backend never touches the docker socket, even with control on
    assert body["docker_socket_enabled"] is False
    manager_cls._instance = None


def test_start_slot_delegates_to_backend_manager(tmp_path, monkeypatch):
    client, manager_cls = _make_app(tmp_path, monkeypatch, control=True)

    calls = []

    async def fake_start(self, name):
        calls.append(name)
        return {
            "name": name,
            "running": True,
            "healthy": True,
            "port": 8080,
            "host": "localhost",
            "container_id": None,
            "pid": None,
            "error": None,
        }

    monkeypatch.setattr(manager_cls, "start_slot", fake_start)

    resp = client.post("/fleet/slots/chat/start")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["action"] == "start"
    assert body["slot"] == "chat"
    assert body["backend"] == "remote"
    assert calls == ["chat"]
    manager_cls._instance = None


def test_start_unknown_slot_returns_404(tmp_path, monkeypatch):
    client, manager_cls = _make_app(tmp_path, monkeypatch, control=True)

    resp = client.post("/fleet/slots/no-such-slot/start")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "unknown_slot"
    manager_cls._instance = None


def test_stop_slot_delegates_and_reports(tmp_path, monkeypatch):
    client, manager_cls = _make_app(tmp_path, monkeypatch, control=True)

    async def fake_stop(self, name):
        return True

    monkeypatch.setattr(manager_cls, "stop_slot", fake_stop)

    resp = client.post("/fleet/slots/utility/stop")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["action"] == "stop"
    assert body["slot"] == "utility"

    resp = client.post("/fleet/slots/no-such-slot/stop")
    assert resp.status_code == 404
    manager_cls._instance = None


def test_start_all_delegates_to_backend_manager(tmp_path, monkeypatch):
    client, manager_cls = _make_app(tmp_path, monkeypatch, control=True)

    async def fake_start_all(self):
        return {
            "chat": {"running": True, "healthy": True, "port": 8080, "error": None},
            "utility": {"running": True, "healthy": True, "port": 8088, "error": None},
        }

    monkeypatch.setattr(manager_cls, "start_all", fake_start_all)

    resp = client.post("/fleet/start")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["action"] == "start_all"
    assert set(body["results"]) == {"chat", "utility"}
    manager_cls._instance = None


def test_control_requires_bearer_token_when_api_key_configured(tmp_path, monkeypatch):
    client, manager_cls = _make_app(tmp_path, monkeypatch, control=True, api_key="local-secret")

    resp = client.post("/fleet/slots/chat/start")
    assert resp.status_code == 401

    async def fake_start(self, name):
        return {"name": name, "running": True, "healthy": True, "port": 8080,
                "host": "localhost", "container_id": None, "pid": None, "error": None}

    monkeypatch.setattr(manager_cls, "start_slot", fake_start)
    resp = client.post(
        "/fleet/slots/chat/start",
        headers={"Authorization": "Bearer local-secret"},
    )
    assert resp.status_code == 200
    manager_cls._instance = None

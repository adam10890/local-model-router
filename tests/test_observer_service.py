"""
Tests for the standalone read-only observer service (service/).

Uses starlette.testclient.TestClient — no real llama.cpp servers required.
SlotHealthChecker and BackendManager are monkeypatched where needed.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Bootstrap sys.path for local_model_router.* imports
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "local_model_router"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from starlette.testclient import TestClient  # noqa: E402

_BASE_CONFIG = """\
active_slots:
  - id: slot_chat
    port: 8080
    host: localhost
    role: chat
    enabled: true
    model_id: mistral-7b
    context_size: 32768
  - id: slot_utility
    port: 8088
    host: localhost
    role: utility
    enabled: true
  - id: slot_disabled
    port: 9999
    host: localhost
    role: utility
    enabled: false
global:
  backend: remote
  lmm_hosts:
    chat: host.docker.internal:8080
    utility: host.docker.internal:8088
  api_key: "should-be-redacted"
  some_token: "also-redacted"
"""

# Config whose slot IDs match DEFAULT_CHAINS so routing preview works without
# custom failover_chains config.
_ROUTING_CONFIG = """\
active_slots:
  - id: chat
    port: 8080
    host: localhost
    role: chat
    enabled: true
  - id: utility
    port: 8088
    host: localhost
    role: utility
    enabled: true
global:
  backend: remote
"""

_EMPTY_CONFIG = "active_slots: []\nglobal:\n  backend: remote\n"


def _make_client(tmp_path, yaml_content=_BASE_CONFIG):
    from local_model_router.service.app import create_app
    cfg = tmp_path / "llama_cpp_servers.yaml"
    cfg.write_text(yaml_content)
    app = create_app(str(cfg))
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    def test_returns_ok_status(self, tmp_path):
        client = _make_client(tmp_path)
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["service"] == "lmm-router-observer"

    def test_includes_version(self, tmp_path):
        client = _make_client(tmp_path)
        body = client.get("/health").json()
        assert "version" in body

    def test_includes_config_path(self, tmp_path):
        client = _make_client(tmp_path)
        body = client.get("/health").json()
        assert "config_path" in body
        assert str(tmp_path) in body["config_path"]

    def test_health_remains_open_when_api_key_configured(self, tmp_path, monkeypatch):
        monkeypatch.setenv("A0_LMM_ROUTER_API_KEY", "local-secret")
        client = _make_client(tmp_path)

        resp = client.get("/health")

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# GET /slots
# ---------------------------------------------------------------------------

class TestSlotsEndpoint:
    def test_returns_configured_slots(self, tmp_path):
        client = _make_client(tmp_path)
        body = client.get("/slots").json()
        ids = [s["id"] for s in body]
        assert "slot_chat" in ids
        assert "slot_utility" in ids

    def test_includes_expected_fields(self, tmp_path):
        client = _make_client(tmp_path)
        slots = client.get("/slots").json()
        chat = next(s for s in slots if s["id"] == "slot_chat")
        assert chat["port"] == 8080
        assert chat["role"] == "chat"
        assert chat["enabled"] is True
        assert "base_url" in chat
        assert "backend_type" in chat

    def test_disabled_slot_is_present_but_marked(self, tmp_path):
        client = _make_client(tmp_path)
        slots = client.get("/slots").json()
        disabled = next(s for s in slots if s["id"] == "slot_disabled")
        assert disabled["enabled"] is False

    def test_empty_config_returns_empty_list(self, tmp_path):
        client = _make_client(tmp_path, _EMPTY_CONFIG)
        body = client.get("/slots").json()
        assert body == []

    def test_does_not_expose_model_paths(self, tmp_path):
        client = _make_client(tmp_path)
        slots = client.get("/slots").json()
        for slot in slots:
            assert "model_path" not in slot or slot.get("model_path") is None

    def test_missing_config_returns_empty_list(self, tmp_path):
        from local_model_router.service.app import create_app
        app = create_app(str(tmp_path / "nonexistent.yaml"))
        client = TestClient(app)
        body = client.get("/slots").json()
        assert body == []


# ---------------------------------------------------------------------------
# GET /config/preview
# ---------------------------------------------------------------------------

class TestConfigPreviewEndpoint:
    def test_returns_sanitized_config(self, tmp_path):
        client = _make_client(tmp_path)
        body = client.get("/config/preview").json()
        assert "active_slots" in body
        assert "global" in body

    def test_api_key_is_redacted(self, tmp_path):
        client = _make_client(tmp_path)
        body = client.get("/config/preview").json()
        global_cfg = body.get("global", {})
        assert global_cfg.get("api_key") == "[REDACTED]"

    def test_token_fields_are_redacted(self, tmp_path):
        client = _make_client(tmp_path)
        body = client.get("/config/preview").json()
        global_cfg = body.get("global", {})
        assert global_cfg.get("some_token") == "[REDACTED]"

    def test_non_sensitive_fields_are_preserved(self, tmp_path):
        client = _make_client(tmp_path)
        body = client.get("/config/preview").json()
        global_cfg = body.get("global", {})
        assert global_cfg.get("backend") == "remote"

    def test_slot_ids_are_visible(self, tmp_path):
        client = _make_client(tmp_path)
        body = client.get("/config/preview").json()
        slot_ids = [s.get("id") for s in body.get("active_slots", [])]
        assert "slot_chat" in slot_ids

    def test_empty_config_returns_empty_dict(self, tmp_path):
        client = _make_client(tmp_path, _EMPTY_CONFIG)
        body = client.get("/config/preview").json()
        assert isinstance(body, dict)


# ---------------------------------------------------------------------------
# GET /routing/preview
# ---------------------------------------------------------------------------

class TestRoutingPreviewEndpoint:
    def _make_obs_with_healthy_checker(self, tmp_path, health_result="healthy"):
        """Return an ObserverBackend whose manager has a stub health checker.

        Uses _ROUTING_CONFIG so slot IDs match DEFAULT_CHAINS.
        """
        from local_model_router.service.observer import ObserverBackend
        from local_model_router.helpers.llama_cpp_manager import BackendManager

        cfg = tmp_path / "llama_cpp_servers.yaml"
        cfg.write_text(_ROUTING_CONFIG)

        BackendManager._instance = None
        mgr = BackendManager(str(cfg))

        _hr = health_result

        class StubChecker:
            async def check_async(self, config):
                return _hr
            def check(self, config):
                return _hr

        mgr._health_checker = StubChecker()

        obs = ObserverBackend(str(cfg))
        obs._make_manager = lambda: mgr  # bypass singleton, inject stub checker
        return obs

    def test_returns_slot_for_healthy_role(self, tmp_path):
        obs = self._make_obs_with_healthy_checker(tmp_path, "healthy")
        result = asyncio.run(obs.get_routing_preview("chat"))
        assert result["role"] == "chat"
        assert result["no_slot_available"] is False
        assert result["slot_id"] is not None
        assert result["url"] is not None
        from local_model_router.helpers.llama_cpp_manager import BackendManager
        BackendManager._instance = None

    def test_returns_no_slot_when_all_unhealthy(self, tmp_path):
        obs = self._make_obs_with_healthy_checker(tmp_path, "unhealthy")
        result = asyncio.run(obs.get_routing_preview("chat"))
        assert result["no_slot_available"] is True
        assert result["slot_id"] is None
        from local_model_router.helpers.llama_cpp_manager import BackendManager
        BackendManager._instance = None

    def test_result_includes_chain(self, tmp_path):
        obs = self._make_obs_with_healthy_checker(tmp_path, "healthy")
        result = asyncio.run(obs.get_routing_preview("chat"))
        assert "chain" in result
        from local_model_router.helpers.llama_cpp_manager import BackendManager
        BackendManager._instance = None

    def test_defaults_to_chat_role_via_http(self, tmp_path, monkeypatch):
        from local_model_router.service.app import create_app
        from local_model_router.helpers.llama_cpp_manager import BackendManager
        BackendManager._instance = None

        async def always_unhealthy(url, timeout):
            return {"ok": False}

        monkeypatch.setattr(
            "local_model_router.helpers.smart_router.health._aiohttp_probe",
            always_unhealthy,
        )
        cfg = tmp_path / "llama_cpp_servers.yaml"
        cfg.write_text(_BASE_CONFIG)
        app = create_app(str(cfg))
        body = TestClient(app).get("/routing/preview").json()
        assert body["role"] == "chat"
        BackendManager._instance = None

    def test_empty_config_returns_no_slot(self, tmp_path):
        from local_model_router.service.observer import ObserverBackend
        from local_model_router.helpers.llama_cpp_manager import BackendManager
        BackendManager._instance = None

        obs = ObserverBackend(str(tmp_path / "nonexistent.yaml"))
        result = asyncio.run(obs.get_routing_preview("chat"))
        assert result["no_slot_available"] is True
        BackendManager._instance = None


# ---------------------------------------------------------------------------
# GET /health/slots
# ---------------------------------------------------------------------------

class TestHealthSlotsEndpoint:
    def test_returns_health_for_each_slot(self, tmp_path, monkeypatch):
        from local_model_router.service.app import create_app

        cfg = tmp_path / "llama_cpp_servers.yaml"
        cfg.write_text(_BASE_CONFIG)

        async def ok_probe(url, timeout):
            return {"ok": True}

        monkeypatch.setattr(
            "local_model_router.helpers.smart_router.health._aiohttp_probe",
            ok_probe,
        )
        body = TestClient(create_app(str(cfg))).get("/health/slots").json()
        assert isinstance(body, list)
        assert len(body) > 0
        for slot in body:
            assert "health" in slot
            assert "id" in slot

    def test_disabled_slot_not_probed(self, tmp_path, monkeypatch):
        from local_model_router.service.app import create_app

        cfg = tmp_path / "llama_cpp_servers.yaml"
        cfg.write_text(_BASE_CONFIG)

        probe_calls = []

        async def tracking_probe(url, timeout):
            probe_calls.append(url)
            return {"ok": True}

        monkeypatch.setattr(
            "local_model_router.helpers.smart_router.health._aiohttp_probe",
            tracking_probe,
        )
        body = TestClient(create_app(str(cfg))).get("/health/slots").json()

        disabled = next(s for s in body if s["id"] == "slot_disabled")
        assert disabled["health"] == "disabled"
        # Port 9999 (disabled slot) must not appear in probe calls
        assert not any("9999" in url for url in probe_calls)

    def test_unreachable_slot_returns_unhealthy_not_crash(self, tmp_path, monkeypatch):
        from local_model_router.service.app import create_app

        cfg = tmp_path / "llama_cpp_servers.yaml"
        cfg.write_text(_BASE_CONFIG)

        async def error_probe(url, timeout):
            raise ConnectionError("ECONNREFUSED")

        monkeypatch.setattr(
            "local_model_router.helpers.smart_router.health._aiohttp_probe",
            error_probe,
        )
        # Must not raise; unreachable slots should appear as unhealthy
        resp = TestClient(create_app(str(cfg))).get("/health/slots")
        assert resp.status_code == 200
        for slot in resp.json():
            if slot.get("enabled"):
                assert slot["health"] == "unhealthy"

    def test_enabled_slots_are_probed_concurrently_and_cached(self, tmp_path, monkeypatch):
        from local_model_router.service.app import create_app

        cfg = tmp_path / "llama_cpp_servers.yaml"
        cfg.write_text(_BASE_CONFIG)
        active = 0
        max_active = 0
        calls = 0

        async def slow_probe(url, timeout):
            nonlocal active, max_active, calls
            calls += 1
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {"ok": True}

        monkeypatch.setattr(
            "local_model_router.helpers.smart_router.health._aiohttp_probe",
            slow_probe,
        )
        client = TestClient(create_app(str(cfg)))

        assert client.get("/health/slots").status_code == 200
        assert client.get("/health/slots").status_code == 200
        assert max_active == 2
        assert calls == 2

    def test_empty_config_returns_empty_list(self, tmp_path):
        from local_model_router.service.app import create_app
        body = TestClient(create_app(str(tmp_path / "nonexistent.yaml"))).get("/health/slots").json()
        assert body == []


# ---------------------------------------------------------------------------
# Service import safety: importing does not start the server
# ---------------------------------------------------------------------------

class TestImportSafety:
    def test_create_app_does_not_start_server(self, tmp_path):
        """Importing and calling create_app must not bind a port."""
        from local_model_router.service.app import create_app
        import socket

        cfg = tmp_path / "llama_cpp_servers.yaml"
        cfg.write_text(_EMPTY_CONFIG)
        create_app(str(cfg))  # must not raise and must not bind port 8096

        # Verify port 8096 is free (the default observer port)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", 8096))  # would fail if port were taken


# ---------------------------------------------------------------------------
# service.__main__ bind safety
# ---------------------------------------------------------------------------

class TestBindAuthGuard:
    def test_public_bind_without_auth_is_rejected(self, monkeypatch):
        from local_model_router.service.__main__ import _validate_bind_auth

        monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
        monkeypatch.delenv("A0_LMM_ROUTER_ALLOW_PUBLIC_NO_AUTH", raising=False)

        try:
            _validate_bind_auth("0.0.0.0")
            assert False, "public no-auth bind should fail"
        except RuntimeError as exc:
            assert "A0_LMM_ROUTER_API_KEY" in str(exc)

    def test_public_bind_with_api_key_is_allowed(self, monkeypatch):
        from local_model_router.service.__main__ import _validate_bind_auth

        monkeypatch.setenv("A0_LMM_ROUTER_API_KEY", "local-secret")
        monkeypatch.delenv("A0_LMM_ROUTER_ALLOW_PUBLIC_NO_AUTH", raising=False)

        _validate_bind_auth("0.0.0.0")

    def test_public_bind_with_explicit_no_auth_override_is_allowed(self, monkeypatch):
        from local_model_router.service.__main__ import _validate_bind_auth

        monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
        monkeypatch.setenv("A0_LMM_ROUTER_ALLOW_PUBLIC_NO_AUTH", "1")

        _validate_bind_auth("0.0.0.0")


# ---------------------------------------------------------------------------
# observer.py unit tests (redaction, slot listing)
# ---------------------------------------------------------------------------

class TestObserverBackend:
    def _make(self, tmp_path, content=_BASE_CONFIG):
        from local_model_router.service.observer import ObserverBackend
        cfg = tmp_path / "llama_cpp_servers.yaml"
        cfg.write_text(content)
        return ObserverBackend(str(cfg))

    def test_get_slots_excludes_model_path(self, tmp_path):
        obs = self._make(tmp_path)
        slots = obs.get_slots()
        for s in slots:
            assert "model_path" not in s

    def test_redact_preserves_non_sensitive(self, tmp_path):
        from local_model_router.service.observer import _redact
        result = _redact({"backend": "remote", "some_token": "abc"})
        assert result["backend"] == "remote"
        assert result["some_token"] == "[REDACTED]"

    def test_redact_is_recursive(self, tmp_path):
        from local_model_router.service.observer import _redact
        result = _redact({"global": {"api_key": "secret", "port": 8080}})
        assert result["global"]["api_key"] == "[REDACTED]"
        assert result["global"]["port"] == 8080

    def test_redact_handles_list_values(self, tmp_path):
        from local_model_router.service.observer import _redact
        result = _redact({"items": [{"api_key": "x"}, {"name": "y"}]})
        assert result["items"][0]["api_key"] == "[REDACTED]"
        assert result["items"][1]["name"] == "y"

    def test_redact_covers_authorization_credentials_and_passwd(self, tmp_path):
        from local_model_router.service.observer import _redact
        result = _redact({
            "authorization": "Bearer x",
            "client_credentials": "y",
            "passwd": "z",
            "backend": "remote",
        })
        assert result["authorization"] == "[REDACTED]"
        assert result["client_credentials"] == "[REDACTED]"
        assert result["passwd"] == "[REDACTED]"
        assert result["backend"] == "remote"

    def test_missing_config_yields_empty_slots(self, tmp_path):
        from local_model_router.service.observer import ObserverBackend
        obs = ObserverBackend(str(tmp_path / "missing.yaml"))
        assert obs.get_slots() == []

    def test_missing_config_yields_empty_preview(self, tmp_path):
        from local_model_router.service.observer import ObserverBackend
        obs = ObserverBackend(str(tmp_path / "missing.yaml"))
        assert obs.get_config_preview() == {}

    def test_routing_preview_no_slot_when_config_missing(self, tmp_path):
        from local_model_router.service.observer import ObserverBackend
        obs = ObserverBackend(str(tmp_path / "missing.yaml"))
        result = asyncio.run(obs.get_routing_preview("chat"))
        assert result["no_slot_available"] is True

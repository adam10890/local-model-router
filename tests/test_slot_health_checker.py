"""
Tests for SlotHealthChecker (helpers/smart_router/health.py).

All tests inject a probe_fn stub — no real network calls.
A separate integration section verifies that BackendManager._get_slot_health()
delegates to self._health_checker, confirming the wiring created in Phase 2.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_MINIMAL_CONFIG = """\
active_slots:
  - id: chat
    port: 8080
    host: localhost
    enabled: true
  - id: utility
    port: 8088
    host: localhost
    enabled: true
global:
  backend: remote
"""


def _make_checker(probe_fn):
    from local_model_router.helpers.smart_router.health import SlotHealthChecker
    return SlotHealthChecker(timeout=1, probe_fn=probe_fn)


def _ok_probe(url, timeout):
    return {"ok": True, "http_status": 200}


def _fail_probe(url, timeout):
    return {"ok": False, "http_status": 503}


def _error_probe(url, timeout):
    return {"ok": False, "error": "connection refused"}


# ---------------------------------------------------------------------------
# SlotHealthChecker.check() return values
# ---------------------------------------------------------------------------

class TestSlotHealthCheckerReturnValues:
    def test_healthy_on_ok_probe(self):
        assert _make_checker(_ok_probe).check({"host": "h", "port": 8080}) == "healthy"

    def test_unhealthy_on_non_200(self):
        assert _make_checker(_fail_probe).check({"host": "h", "port": 8080}) == "unhealthy"

    def test_unhealthy_on_connection_error(self):
        assert _make_checker(_error_probe).check({"host": "h", "port": 8080}) == "unhealthy"

    def test_unknown_when_port_missing(self):
        # No port key → UNKNOWN (cannot build a valid URL)
        assert _make_checker(_ok_probe).check({"host": "localhost"}) == "unknown"

    def test_unknown_when_port_is_none(self):
        assert _make_checker(_ok_probe).check({"host": "localhost", "port": None}) == "unknown"

    def test_unknown_when_port_is_zero(self):
        assert _make_checker(_ok_probe).check({"host": "localhost", "port": 0}) == "unknown"

    def test_host_defaults_to_localhost(self):
        received = {}

        def capture(url, timeout):
            received["url"] = url
            return {"ok": True}

        _make_checker(capture).check({"port": 9090})
        assert "localhost:9090" in received["url"]


# ---------------------------------------------------------------------------
# Probe receives correct URL and timeout
# ---------------------------------------------------------------------------

class TestSlotHealthCheckerProbeArgs:
    def test_url_constructed_correctly(self):
        received = {}

        def capture(url, timeout):
            received["url"] = url
            return {"ok": True}

        _make_checker(capture).check({"host": "myhost", "port": 7777})
        assert received["url"] == "http://myhost:7777/health"

    def test_timeout_passed_through(self):
        received = {}

        def capture(url, timeout):
            received["timeout"] = timeout
            return {"ok": True}

        from local_model_router.helpers.smart_router.health import SlotHealthChecker
        SlotHealthChecker(timeout=5, probe_fn=capture).check({"host": "h", "port": 1234})
        assert received["timeout"] == 5


# ---------------------------------------------------------------------------
# Probe_fn that raises an exception must not crash the router
# ---------------------------------------------------------------------------

class TestSlotHealthCheckerExceptionSafety:
    def test_raising_probe_returns_unhealthy(self):
        def exploding_probe(url, timeout):
            raise RuntimeError("unexpected crash in probe")

        result = _make_checker(exploding_probe).check({"host": "h", "port": 8080})
        assert result == "unhealthy"

    def test_raising_probe_does_not_propagate(self):
        def exploding_probe(url, timeout):
            raise ConnectionError("ECONNREFUSED")

        # Must not raise
        _make_checker(exploding_probe).check({"host": "h", "port": 8080})


# ---------------------------------------------------------------------------
# _urllib_probe unit (behavior contract, no real network)
# ---------------------------------------------------------------------------

class TestUrllibProbeContract:
    """Verify _urllib_probe returns the right shape when mocked at urllib level."""

    def test_ok_response_returns_ok_true(self, monkeypatch):
        import json as _json

        class FakeResp:
            status = 200
            def read(self): return _json.dumps({"status": "ok"}).encode()
            def __enter__(self): return self
            def __exit__(self, *a): pass

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout: FakeResp())

        from local_model_router.helpers.smart_router.health import _urllib_probe
        result = _urllib_probe("http://localhost:8080/health", 2)
        assert result["ok"] is True
        assert result["http_status"] == 200

    def test_non_ok_status_returns_ok_false(self, monkeypatch):
        import json as _json

        class FakeResp:
            status = 200
            def read(self): return _json.dumps({"status": "loading"}).encode()
            def __enter__(self): return self
            def __exit__(self, *a): pass

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout: FakeResp())

        from local_model_router.helpers.smart_router.health import _urllib_probe
        result = _urllib_probe("http://localhost:8080/health", 2)
        assert result["ok"] is False

    def test_http_503_returns_ok_false(self, monkeypatch):
        class FakeResp:
            status = 503
            def read(self): return b""
            def __enter__(self): return self
            def __exit__(self, *a): pass

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout: FakeResp())

        from local_model_router.helpers.smart_router.health import _urllib_probe
        result = _urllib_probe("http://localhost:8080/health", 2)
        assert result["ok"] is False

    def test_exception_returns_ok_false_with_error_key(self, monkeypatch):
        import urllib.request
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda url, timeout: (_ for _ in ()).throw(OSError("connection refused")),
        )

        from local_model_router.helpers.smart_router.health import _urllib_probe
        result = _urllib_probe("http://localhost:8080/health", 2)
        assert result["ok"] is False
        assert "error" in result

    def test_malformed_json_returns_ok_false(self, monkeypatch):
        class FakeResp:
            status = 200
            def read(self): return b"not json {"
            def __enter__(self): return self
            def __exit__(self, *a): pass

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout: FakeResp())

        from local_model_router.helpers.smart_router.health import _urllib_probe
        result = _urllib_probe("http://localhost:8080/health", 2)
        # malformed JSON → exception caught inside _urllib_probe → ok=False
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# Integration: BackendManager._get_slot_health delegates to _health_checker
# ---------------------------------------------------------------------------

class TestBackendManagerHealthIntegration:
    """Verify that BackendManager._get_slot_health() calls self._health_checker.check()."""

    def _make_manager(self, tmp_path):
        from local_model_router.helpers.llama_cpp_manager import BackendManager
        BackendManager._instance = None
        cfg = tmp_path / "llama_cpp_servers.yaml"
        cfg.write_text(_MINIMAL_CONFIG)
        return BackendManager(str(cfg))

    def test_delegates_to_health_checker(self, tmp_path):
        manager = self._make_manager(tmp_path)
        calls = []

        class StubChecker:
            def check(self, config):
                calls.append(config.get("port"))
                return "healthy"

        manager._health_checker = StubChecker()
        result = manager._get_slot_health("chat")

        assert result == "healthy"
        assert calls == [8080]

    def test_cooldown_slot_bypasses_health_checker(self, tmp_path):
        manager = self._make_manager(tmp_path)
        probe_called = []

        class StubChecker:
            def check(self, config):
                probe_called.append(True)
                return "healthy"

        manager._health_checker = StubChecker()
        manager._cooldown_tracker.mark_error("chat", "forced error")

        result = manager._get_slot_health("chat")

        assert result == "unhealthy"
        assert probe_called == []  # cooldown check short-circuits before HTTP probe

    def test_unknown_slot_returns_unknown(self, tmp_path):
        manager = self._make_manager(tmp_path)
        result = manager._get_slot_health("nonexistent_slot")
        assert result == "unknown"

    def test_no_backend_returns_unknown(self, tmp_path):
        manager = self._make_manager(tmp_path)
        manager._backend = None
        result = manager._get_slot_health("chat")
        assert result == "unknown"

    def test_health_checker_timeout_from_config(self, tmp_path):
        from local_model_router.helpers.llama_cpp_manager import BackendManager
        BackendManager._instance = None
        cfg = tmp_path / "llama_cpp_servers.yaml"
        cfg.write_text(
            "active_slots: []\nglobal:\n  backend: remote\n  health_check_timeout: 7\n"
        )
        manager = BackendManager(str(cfg))
        assert manager._health_checker.timeout == 7


# ---------------------------------------------------------------------------
# check_async tests (all inject async_probe_fn — no real network)
# ---------------------------------------------------------------------------

class TestSlotHealthCheckerAsync:
    def _make_async_checker(self, async_probe_fn):
        from local_model_router.helpers.smart_router.health import SlotHealthChecker
        return SlotHealthChecker(timeout=1, async_probe_fn=async_probe_fn)

    def test_check_async_healthy_on_ok_probe(self):
        import asyncio

        async def ok_probe(url, timeout):
            return {"ok": True, "http_status": 200}

        checker = self._make_async_checker(ok_probe)
        result = asyncio.run(checker.check_async({"host": "h", "port": 8080}))
        assert result == "healthy"

    def test_check_async_unhealthy_on_fail_probe(self):
        import asyncio

        async def fail_probe(url, timeout):
            return {"ok": False, "http_status": 503}

        result = asyncio.run(
            self._make_async_checker(fail_probe).check_async({"host": "h", "port": 8080})
        )
        assert result == "unhealthy"

    def test_check_async_unhealthy_on_connection_error(self):
        import asyncio

        async def error_probe(url, timeout):
            return {"ok": False, "error": "connection refused"}

        result = asyncio.run(
            self._make_async_checker(error_probe).check_async({"host": "h", "port": 8080})
        )
        assert result == "unhealthy"

    def test_check_async_unknown_when_port_missing(self):
        import asyncio

        async def ok_probe(url, timeout):
            return {"ok": True}

        result = asyncio.run(
            self._make_async_checker(ok_probe).check_async({"host": "localhost"})
        )
        assert result == "unknown"

    def test_check_async_timeout_passed_through(self):
        import asyncio

        received = {}

        async def capture_probe(url, timeout):
            received["timeout"] = timeout
            return {"ok": True}

        from local_model_router.helpers.smart_router.health import SlotHealthChecker
        checker = SlotHealthChecker(timeout=9, async_probe_fn=capture_probe)
        asyncio.run(checker.check_async({"host": "h", "port": 1234}))
        assert received["timeout"] == 9

    def test_check_async_url_constructed_correctly(self):
        import asyncio

        received = {}

        async def capture_probe(url, timeout):
            received["url"] = url
            return {"ok": True}

        asyncio.run(
            self._make_async_checker(capture_probe).check_async({"host": "myhost", "port": 7777})
        )
        assert received["url"] == "http://myhost:7777/health"

    def test_check_async_raising_probe_returns_unhealthy(self):
        import asyncio

        async def exploding_probe(url, timeout):
            raise ConnectionError("ECONNREFUSED")

        result = asyncio.run(
            self._make_async_checker(exploding_probe).check_async({"host": "h", "port": 8080})
        )
        assert result == "unhealthy"

    def test_check_async_raising_probe_does_not_propagate(self):
        import asyncio

        async def exploding_probe(url, timeout):
            raise RuntimeError("unexpected crash")

        # Must not raise
        asyncio.run(
            self._make_async_checker(exploding_probe).check_async({"host": "h", "port": 8080})
        )

    def test_check_async_malformed_response_returns_unhealthy(self):
        import asyncio

        async def malformed_probe(url, timeout):
            # ok key missing entirely
            return {"http_status": 200}

        result = asyncio.run(
            self._make_async_checker(malformed_probe).check_async({"host": "h", "port": 8080})
        )
        assert result == "unhealthy"

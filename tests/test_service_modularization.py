"""Tests for service modularization, SharedHttpClient pooling, and orchestrator sunset."""
from __future__ import annotations

import asyncio
import sys

import aiohttp
from starlette.testclient import TestClient

from local_model_router.service.app import create_app
from local_model_router.service.http_client import SharedHttpClient


def _write_minimal_config(tmp_path):
    conf = tmp_path / "llama_cpp_servers.yaml"
    conf.write_text("global:\n  backend: remote\nactive_slots: []\n", encoding="utf-8")
    (tmp_path / "apps.yaml").write_text("apps: {}\n", encoding="utf-8")
    (tmp_path / "harnesses.yaml").write_text("harnesses: {}\n", encoding="utf-8")
    (tmp_path / "upstreams.yaml").write_text("upstreams: {}\n", encoding="utf-8")
    return conf


def test_shared_http_client_reuses_real_session_and_closes_cleanly():
    async def scenario():
        pool = SharedHttpClient()
        try:
            async with pool.request_session() as s1:
                async with pool.request_session() as s2:
                    s3, should_close = await pool.open_stream_session()
                    assert isinstance(s1, aiohttp.ClientSession)
                    assert s1 is s2
                    assert s3 is s1
                    assert should_close is False
                    assert not s1.closed
        finally:
            await pool.close()
        assert s1.closed

    asyncio.run(scenario())


def test_shared_http_client_recreates_session_across_event_loops():
    pool = SharedHttpClient()
    seen_ids: list[int] = []

    async def grab():
        async with pool.request_session() as s:
            seen_ids.append(id(s))
            assert not s.closed

    asyncio.run(grab())
    asyncio.run(grab())
    assert len(seen_ids) == 2
    assert seen_ids[0] != seen_ids[1]

    async def cleanup():
        await pool.close()

    asyncio.run(cleanup())


def test_shared_http_client_falls_back_when_aiohttp_client_session_is_patched(monkeypatch):
    created: list[object] = []

    class FakeSession:
        def __init__(self):
            self.closed = False
            created.append(self)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: FakeSession())

    async def scenario():
        pool = SharedHttpClient()
        assert pool.is_patched() is True
        async with pool.request_session(timeout=aiohttp.ClientTimeout(total=3)) as s1:
            assert not s1.closed
        assert s1.closed is True

        s2, should_close = await pool.open_stream_session()
        assert should_close is True
        assert s2 is not s1
        assert not s2.closed
        await s2.close()
        assert s2.closed is True
        await pool.close()

    asyncio.run(scenario())
    assert len(created) == 2


def test_starlette_lifespan_closes_shared_http_client(tmp_path, monkeypatch):
    conf = _write_minimal_config(tmp_path)
    monkeypatch.setenv("IMPERIUM_ENABLE_LEGACY_ORCHESTRATOR", "0")
    app = create_app(str(conf), setup_home=str(tmp_path / "setup"))
    pool = app.state.context.http_client

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        # Open the pooled session inside the lifespan loop
        client.portal.call(pool._get_or_create_shared_session)  # noqa: SLF001
        session = pool._session  # noqa: SLF001
        assert session is not None
        assert not session.closed

    assert pool._session is None  # noqa: SLF001
    assert session.closed is True


def test_legacy_orchestrator_default_enabled_adds_deprecation_and_sunset_headers(tmp_path, monkeypatch):
    conf = _write_minimal_config(tmp_path)
    monkeypatch.delenv("IMPERIUM_ENABLE_LEGACY_ORCHESTRATOR", raising=False)
    monkeypatch.delenv("A0_LMM_ROUTER_ENABLE_LEGACY_ORCHESTRATOR", raising=False)
    app = create_app(
        str(conf),
        setup_home=str(tmp_path / "setup"),
    )
    with TestClient(app) as client:
        resp = client.get("/orchestrator/summary")
        assert resp.status_code == 200
        assert resp.headers.get("Deprecation") == "true"
        assert resp.headers.get("Sunset") == "Wed, 31 Dec 2026 23:59:59 GMT"


def test_legacy_orchestrator_disabled_returns_404_while_router_stays_healthy(tmp_path, monkeypatch):
    conf = _write_minimal_config(tmp_path)
    for env_name in ("IMPERIUM_ENABLE_LEGACY_ORCHESTRATOR", "A0_LMM_ROUTER_ENABLE_LEGACY_ORCHESTRATOR"):
        monkeypatch.delenv("IMPERIUM_ENABLE_LEGACY_ORCHESTRATOR", raising=False)
        monkeypatch.delenv("A0_LMM_ROUTER_ENABLE_LEGACY_ORCHESTRATOR", raising=False)
        monkeypatch.setenv(env_name, "0")
        app = create_app(str(conf), setup_home=str(tmp_path / "setup"))
        assert app.state.context.agent_orchestrator is None
        with TestClient(app) as client:
            assert client.get("/orchestrator/summary").status_code == 404
            assert client.post("/orchestrator/plans", json={"title": "x"}).status_code == 404
            assert client.get("/health").status_code == 200
            assert client.get("/v1/models").status_code == 200


def test_service_app_reexports_expected_monkeypatch_hooks():
    import local_model_router.service.app as app_module

    for symbol in (
        "SetupEngine",
        "ObserverBackend",
        "scan_hardware",
        "inspect_running_llama_models",
        "run_agent",
        "collect_doctor_checks",
        "_context_for_slot",
        "_record_upstream_usage",
        "_stream_upstream_response",
    ):
        assert hasattr(app_module, symbol)
    assert sys.modules["local_model_router.service.app"] is app_module

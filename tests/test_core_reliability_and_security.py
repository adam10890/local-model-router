"""Tests for PR 1 core reliability, SQLite hardening, and security invariants."""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import textwrap
from pathlib import Path

from starlette.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_model_router import structured_output as so  # noqa: E402
from local_model_router.helpers import stats_tracker, usage_ledger  # noqa: E402
from local_model_router.helpers.llama_cpp_manager import BackendManager  # noqa: E402
from local_model_router.service.app import create_app  # noqa: E402
from local_model_router.service.fleet_manager import (  # noqa: E402
    AgentIdentity,
    FleetQueue,
    FleetStore,
)


_FLEET_YAML = textwrap.dedent("""
    active_slots:
      - id: chat
        host: localhost
        port: 8080
        role: chat
        enabled: true
        model_id: chat-model
        context_size: 32768
      - id: embed
        host: localhost
        port: 8082
        role: embed
        enabled: true
        model_id: embed-model
        context_size: 8192
    global:
      backend: remote
""")

_UPSTREAMS_YAML = textwrap.dedent("""
    upstreams:
      - name: up
        type: openai_compatible
        base_url: http://localhost:11434/v1
        enabled: true
        capabilities: [chat, models, json_mode]
        max_active: 1
        max_queue: 2
""")


def _write(tmp_path: Path, name: str, content: str) -> str:
    target = tmp_path / name
    target.write_text(content, encoding="utf-8")
    return str(target)


def _patch_healthy(monkeypatch):
    async def healthy(_url, _timeout):
        return {"ok": True}

    monkeypatch.setattr(
        "local_model_router.helpers.smart_router.health._aiohttp_probe",
        healthy,
    )


def _app_and_store(tmp_path: Path, monkeypatch, *, queue=None, api_key: str | None = None):
    BackendManager._instance = None
    _patch_healthy(monkeypatch)
    if api_key is None:
        monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    else:
        monkeypatch.setenv("A0_LMM_ROUTER_API_KEY", api_key)
    store = FleetStore(str(tmp_path / "fleet.sqlite3"))
    app = create_app(
        _write(tmp_path, "llama_cpp_servers.yaml", _FLEET_YAML),
        fleet_store=store,
        fleet_queue=queue,
        upstreams_path=_write(tmp_path, "upstreams.yaml", _UPSTREAMS_YAML),
        apps_path=str(tmp_path / "missing-apps.yaml"),
        harnesses_path=str(tmp_path / "missing-harnesses.yaml"),
    )
    return app, store


def _reply(content: str, *, prompt_tokens: int = 10, completion_tokens: int = 5):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def test_upstream_structured_output_runs_before_finalize_and_records_422_failure(tmp_path, monkeypatch):
    import aiohttp

    monkeypatch.setenv(so.ENFORCE_ENV, "1")
    monkeypatch.setattr(usage_ledger, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    usage_ledger._cache.clear()

    app, store = _app_and_store(tmp_path, monkeypatch)
    replies = [_reply("not json"), _reply("still not json")]
    recorded_active_counts: list[int] = []

    class FakeResponse:
        status = 200

        def __init__(self, payload):
            self._payload = payload

        async def json(self, content_type=None):
            return self._payload

        async def text(self):
            return ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        def post(self, *_a, **_kw):
            # Capture active upstream admission count when each forward happens.
            recorded_active_counts.append(1)
            return FakeResponse(replies.pop(0))

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *_a, **_kw: FakeSession())
    client = TestClient(app)

    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "up/test-model",
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_object"},
        },
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == so.FAILED
    assert len(recorded_active_counts) == 2
    recent = store.routing_analytics()["recent"][0]
    assert recent["status"] == "failed"
    assert recent["error_code"] == so.FAILED
    assert usage_ledger.window_totals("up", 3600)["requests"] == 0


def test_upstream_structured_output_repair_preserves_completed_status(tmp_path, monkeypatch):
    import aiohttp

    monkeypatch.setenv(so.ENFORCE_ENV, "1")
    monkeypatch.setattr(usage_ledger, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    usage_ledger._cache.clear()

    app, store = _app_and_store(tmp_path, monkeypatch)
    replies = [_reply("prose"), _reply('{"ok": true}', prompt_tokens=12, completion_tokens=4)]

    class FakeResponse:
        status = 200

        def __init__(self, payload):
            self._payload = payload

        async def json(self, content_type=None):
            return self._payload

        async def text(self):
            return ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        def post(self, *_a, **_kw):
            return FakeResponse(replies.pop(0))

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *_a, **_kw: FakeSession())
    client = TestClient(app)

    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "up/test-model",
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_object"},
        },
    )

    assert resp.status_code == 200
    assert resp.headers["x-a0-router-structured-output"] == so.REPAIRED
    recent = store.routing_analytics()["recent"][0]
    assert recent["status"] == "completed"
    assert recent["prompt_tokens"] == 12
    assert usage_ledger.window_totals("up", 3600)["tokens"] == 16


def test_upstream_stream_split_done_and_early_disconnect_release(tmp_path, monkeypatch):
    import aiohttp

    app, store = _app_and_store(tmp_path, monkeypatch)
    sessions: list[object] = []

    class SplitDoneContent:
        async def iter_chunked(self, _size):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            yield b"data: [DO"
            yield b"NE]\n\n"

    class FakeResponse:
        status = 200

        def __init__(self, content):
            self.content = content
            self.released = False

        def release(self):
            self.released = True

    class FakeSession:
        def __init__(self, content):
            self._content = content
            self.closed = False
            self.response = FakeResponse(content)
            sessions.append(self)

        async def post(self, *_a, **_kw):
            return self.response

        async def close(self):
            self.closed = True

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *_a, **_kw: FakeSession(SplitDoneContent()))
    client = TestClient(app)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "up/test-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes())
    assert b"data: [DONE]" in body
    assert sessions[0].closed is True
    assert sessions[0].response.released is True
    assert store.routing_analytics()["recent"][0]["status"] == "completed"

    # Now verify upstream stream error still finalizes and cleans up via ManagedStreamingResponse.
    class BrokenContent:
        async def iter_chunked(self, _size):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise aiohttp.ClientConnectionError("reset")

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *_a, **_kw: FakeSession(BrokenContent()))
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "up/test-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as resp:
        assert resp.status_code == 200
        _ = b"".join(resp.iter_bytes())
    assert sessions[1].closed is True
    assert sessions[1].response.released is True
    assert store.routing_analytics()["recent"][0]["status"] == "failed"
    assert store.routing_analytics()["recent"][0]["error_code"] == "upstream_stream_error"
    # Upstream admission lane is completely free again.
    status_body = client.get("/fleet/status").json()
    assert status_body["queues"]["upstream:up"]["active"] == 0

    # Also verify early ASGI client disconnect cleans up via _ManagedStreamingResponse.on_close.
    import json as _json
    import pytest
    from starlette.requests import ClientDisconnect

    class HangingContent:
        async def iter_chunked(self, _size):
            yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
            await asyncio.sleep(5)

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *_a, **_kw: FakeSession(HangingContent()))

    async def disconnect_stream():
        payload = _json.dumps({
            "model": "up/test-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }).encode("utf-8")
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": payload, "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                raise OSError("client disconnected")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 10000),
            "server": ("127.0.0.1", 9000),
            "root_path": "",
        }
        with pytest.raises((OSError, ClientDisconnect, BaseExceptionGroup)):
            await app(scope, receive, send)

    asyncio.run(disconnect_stream())
    assert sessions[2].closed is True
    assert sessions[2].response.released is True
    assert store.routing_analytics()["recent"][0]["status"] == "failed"
    assert store.routing_analytics()["recent"][0]["error_code"] == "client_disconnected"
    assert client.get("/fleet/status").json()["queues"]["upstream:up"]["active"] == 0


def test_embeddings_admission_swap_error_handling_and_telemetry(tmp_path, monkeypatch):
    import aiohttp

    # 1. Queue full -> 429
    app_full, store_full = _app_and_store(
        tmp_path / "full",
        monkeypatch,
        queue=FleetQueue(max_active=0, max_queue=0),
    )
    (tmp_path / "full").mkdir(exist_ok=True)
    resp_429 = TestClient(app_full).post(
        "/v1/embeddings",
        json={"model": "embedding", "input": ["hi"]},
    )
    assert resp_429.status_code == 429
    assert resp_429.json()["error"]["code"] == "queue_full"
    assert store_full.request_summary()["by_status"]["rejected"] == 1

    # 2. Timeout -> 504 and queue released; Invalid JSON -> 502; Valid -> 200 + telemetry
    (tmp_path / "ok").mkdir(exist_ok=True)
    app, store = _app_and_store(
        tmp_path / "ok",
        monkeypatch,
        queue=FleetQueue(max_active=1, max_queue=1),
    )
    mode = {"kind": "timeout"}

    class FakeResponse:
        status = 200

        async def json(self, content_type=None):
            if mode["kind"] == "invalid_json":
                raise ValueError("bad json")
            return {
                "object": "list",
                "data": [{"embedding": [0.1, 0.2], "index": 0}],
                "usage": {"prompt_tokens": 7, "total_tokens": 7},
            }

        async def text(self):
            return "not-json"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    class FakeSession:
        def post(self, *_a, **_kw):
            if mode["kind"] == "timeout":
                raise TimeoutError("timed out")
            return FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *_a, **_kw: FakeSession())
    client = TestClient(app)

    r_timeout = client.post("/v1/embeddings", json={"model": "embedding", "input": ["a"]})
    assert r_timeout.status_code == 504
    assert r_timeout.json()["error"]["code"] == "upstream_timeout"

    mode["kind"] = "invalid_json"
    r_bad = client.post("/v1/embeddings", json={"model": "embedding", "input": ["a"]})
    assert r_bad.status_code == 502
    assert r_bad.json()["error"]["code"] == "upstream_invalid_json"

    mode["kind"] = "ok"
    r_ok = client.post("/v1/embeddings", json={"model": "embedding", "input": ["a"]})
    assert r_ok.status_code == 200
    assert r_ok.headers["x-a0-admission-lane"] == "local"
    assert r_ok.headers["x-a0-request-id"].startswith("req_")
    recent = store.routing_analytics()["recent"][0]
    assert recent["status"] == "completed"
    assert recent["prompt_tokens"] == 7
    assert client.get("/fleet/status").json()["queues"]["local"]["active"] == 0


def test_embeddings_rejects_during_slot_swap(tmp_path, monkeypatch):
    BackendManager._instance = None
    _patch_healthy(monkeypatch)
    monkeypatch.setenv("A0_LMM_ROUTER_ENABLE_FLEET_CONTROL", "1")
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    cfg = _write(
        tmp_path,
        "llama_cpp_servers.yaml",
        textwrap.dedent("""
            active_slots:
              - id: embed
                host: localhost
                port: 8082
                role: embed
                enabled: true
                model_id: embed-model
                context_size: 8192
            global:
              backend: subprocess
        """),
    )
    app = create_app(
        cfg,
        fleet_store=FleetStore(str(tmp_path / "fleet.sqlite3")),
        upstreams_path=_write(tmp_path, "upstreams.yaml", _UPSTREAMS_YAML),
        apps_path=str(tmp_path / "missing-apps.yaml"),
        harnesses_path=str(tmp_path / "missing-harnesses.yaml"),
    )
    assert app.state.backend_manager.begin_slot_operation("embed", "loading")
    resp = TestClient(app).post("/v1/embeddings", json={"model": "embedding", "input": ["a"]})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "gpu_swap_in_progress"
    assert resp.headers["Retry-After"] == "5"
    app.state.backend_manager.finish_slot_operation("embed")
    BackendManager._instance = None


def test_security_headers_health_redaction_401_envelope_and_csrf_guard(tmp_path, monkeypatch):
    # 1. With API key: /health hides config_path when unauthenticated, shows when authorized.
    app_auth, _ = _app_and_store(tmp_path / "auth", monkeypatch, api_key="secret-token")
    (tmp_path / "auth").mkdir(exist_ok=True)
    # Re-create with existing directory
    app_auth, _ = _app_and_store(tmp_path / "auth", monkeypatch, api_key="secret-token")
    c_auth = TestClient(app_auth)

    unauth_health = c_auth.get("/health")
    assert unauth_health.status_code == 200
    assert "config_path" not in unauth_health.json()

    auth_health = c_auth.get("/health", headers={"Authorization": "Bearer secret-token"})
    assert auth_health.status_code == 200
    assert "config_path" in auth_health.json()

    # Harness /v1/ endpoints return OpenAI error JSON on 401.
    harness_401 = c_auth.post(
        "/harnesses/hermes/v1/chat/completions",
        json={"model": "local", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert harness_401.status_code == 401
    assert harness_401.json()["error"]["code"] == "unauthorized"
    assert harness_401.json()["error"]["type"] == "authentication_error"

    # Cross-origin request WITH valid bearer token is permitted.
    ok_with_auth = c_auth.post(
        "/fleet/agents/register",
        headers={
            "Authorization": "Bearer secret-token",
            "Origin": "https://dashboard.example",
        },
        json={"agent_id": "a1"},
    )
    assert ok_with_auth.status_code == 200

    # 2. Without API key: /ui returns security headers, localhost CSRF guard blocks evil origins & simple types.
    (tmp_path / "open").mkdir(exist_ok=True)
    app_open, _ = _app_and_store(tmp_path / "open", monkeypatch, api_key=None)
    c_open = TestClient(app_open)

    ui = c_open.get("/ui")
    assert ui.status_code == 200
    assert "Content-Security-Policy" in ui.headers
    assert ui.headers["X-Content-Type-Options"] == "nosniff"
    assert ui.headers["X-Frame-Options"] == "DENY"
    assert ui.headers["Referrer-Policy"] == "no-referrer"

    csrf_evil = c_open.post(
        "/fleet/agents/register",
        headers={"Origin": "https://evil.example"},
        json={"agent_id": "a1"},
    )
    assert csrf_evil.status_code == 403
    assert csrf_evil.json()["error"] == "cross_origin_forbidden"

    csrf_null = c_open.post(
        "/fleet/agents/register",
        headers={"Origin": "null"},
        json={"agent_id": "a1"},
    )
    assert csrf_null.status_code == 403

    csrf_plain = c_open.post(
        "/fleet/agents/register",
        headers={"Origin": "http://127.0.0.1:3000", "Content-Type": "text/plain"},
        content='{"agent_id":"a1"}',
    )
    assert csrf_plain.status_code == 415
    assert csrf_plain.json()["error"] == "unsupported_media_type"

    local_ok = c_open.post(
        "/fleet/agents/register",
        headers={"Origin": "http://127.0.0.1:9000", "Content-Type": "application/json"},
        json={"agent_id": "a1"},
    )
    assert local_ok.status_code == 200


def test_fleet_store_wal_indexes_dedup_snapshots_and_prune(tmp_path, monkeypatch):
    app, store = _app_and_store(tmp_path, monkeypatch)
    db_file = tmp_path / "fleet.sqlite3"
    with sqlite3.connect(db_file) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        indexes = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
        }
    assert str(mode).lower() == "wal"
    assert {
        "idx_requests_updated_at",
        "idx_requests_status",
        "idx_queue_events_created_at",
        "idx_snapshots_source_id",
    }.issubset(indexes)

    client = TestClient(app)
    assert client.get("/fleet/status").status_code == 200
    assert client.get("/fleet/status").status_code == 200
    with sqlite3.connect(db_file) as conn:
        snap_count = conn.execute(
            "SELECT COUNT(*) FROM model_residency_snapshots WHERE source = 'observer_slots'"
        ).fetchone()[0]
    assert snap_count == 1

    # Prune bounds requests, queue events, and snapshots per source independently.
    for i in range(4):
        req_id = store.create_request(AgentIdentity(agent_id=f"a{i}"))
        store.record_queue_event(
            request_id=req_id,
            agent=AgentIdentity(agent_id=f"a{i}"),
            event_type="admitted",
            queue_depth=0,
            active_count=1,
        )
        store.record_model_snapshot("observer_slots", {"i": i})
    store.record_model_snapshot("model_evaluation", {"schema_version": 1})

    store.prune(max_requests=2, max_queue_events=2, max_snapshots_per_source=1)
    with sqlite3.connect(db_file) as conn:
        req_n = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        qe_n = conn.execute("SELECT COUNT(*) FROM queue_events").fetchone()[0]
        obs_n = conn.execute(
            "SELECT COUNT(*) FROM model_residency_snapshots WHERE source = 'observer_slots'"
        ).fetchone()[0]
        eval_n = conn.execute(
            "SELECT COUNT(*) FROM model_residency_snapshots WHERE source = 'model_evaluation'"
        ).fetchone()[0]
    assert req_n == 2
    assert qe_n == 2
    assert obs_n == 1
    assert eval_n == 1


def test_stats_tracker_defaults_outside_package_and_writes_atomically(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_STATS_PATH", raising=False)
    monkeypatch.setenv("IMPERIUM_HOME", str(tmp_path / "imperium-home"))
    monkeypatch.setattr(stats_tracker, "_stats", None)
    monkeypatch.setattr(stats_tracker, "_stats_path", None)

    stats_tracker.record_failover("chat", "utility", "unhealthy")
    expected = tmp_path / "imperium-home" / "state" / "failover_stats.json"
    assert expected.is_file()
    leftover_tmps = list(expected.parent.glob("*.tmp"))
    assert leftover_tmps == []
    assert asyncio.run(_run_cookbook_check(tmp_path, monkeypatch)) == 200


async def _run_cookbook_check(tmp_path: Path, monkeypatch):
    app, _ = _app_and_store(tmp_path / "cb", monkeypatch)
    # Just verify /cookbook runs cleanly through to_thread path without error.
    (tmp_path / "cb").mkdir(exist_ok=True)
    app, _ = _app_and_store(tmp_path / "cb", monkeypatch)
    resp = TestClient(app).get("/cookbook")
    return resp.status_code

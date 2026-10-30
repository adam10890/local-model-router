"""Pinned local Scribe, independent admission, and metadata-only telemetry.

The local Scribe queue isolates scheduling from chat; it does not reserve
physical GPU resources. Privacy intent also applies to explicit upstream pins.

Hermetic: no fleet, GPU, Docker, or network.
    python -m pytest tests/test_scribe_integration.py -q
"""
from __future__ import annotations

import asyncio
import textwrap

import httpx
import pytest

from starlette.testclient import TestClient

from local_model_router.service.app import create_app
from local_model_router.service.fleet_manager import FleetQueue, FleetStore


@pytest.fixture(autouse=True)
def _reset_manager():
    from local_model_router.helpers.llama_cpp_manager import BackendManager

    BackendManager._instance = None
    yield
    BackendManager._instance = None

_FLEET = """\
active_slots:
  - id: chat
    host: localhost
    port: 8080
    role: chat
    enabled: true
    model_id: chat-model
  - id: scribe_gpu
    host: localhost
    port: 8090
    role: scribe
    enabled: true
    model_id: scribe-model
    trust_tier: local_aligned
global:
  backend: remote
"""

_HARNESSES = """\
harnesses:
  a0_scribe:
    display_name: A0 Scribe
    kind: a0_scribe
    protocol: openai
    location: docker
    connections:
      default: {model: scribe-model}
"""

SCRIBE_HEADERS = {
    "X-Agent-Id": "a0_scribe",
    "X-Agent-Type": "scribe",
    "X-Priority": "low",
    "X-App-Id": "a0_scribe",
}
SCRIBE_URL = "/harnesses/a0_scribe/v1/chat/completions"
CONTENT_CANARY = "scribe-content-canary-7f3a"


def _write(path, content):
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return str(path)


def _scribe_body():
    return {
        "model": "local",
        "messages": [
            {"role": "system", "content": "You are the Scribe."},
            {"role": "user", "content": f"Agent activity log: {CONTENT_CANARY}"},
        ],
        "max_tokens": 64,
        "stream": False,
        "routing": {"role": "scribe", "task_type": "documentation", "local_only": True},
    }


def _client(tmp_path, monkeypatch, *, queue=None, scribe_healthy=True, **app_kwargs):
    from local_model_router.helpers.llama_cpp_manager import BackendManager

    BackendManager._instance = None
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)

    async def health_probe(url, timeout):
        return {"ok": scribe_healthy or ":8090" not in url}

    monkeypatch.setattr(
        "local_model_router.helpers.smart_router.health._aiohttp_probe", health_probe
    )
    db_path = tmp_path / "fleet.sqlite3"
    app = create_app(
        _write(tmp_path / "llama_cpp_servers.yaml", _FLEET),
        harnesses_path=_write(tmp_path / "harnesses.yaml", _HARNESSES),
        upstreams_path=str(tmp_path / "missing-upstreams.yaml"),
        apps_path=str(tmp_path / "missing-apps.yaml"),
        fleet_store=FleetStore(str(db_path)),
        fleet_queue=queue or FleetQueue(max_active=1, max_queue=4),
        **app_kwargs,
    )
    return TestClient(app), db_path, BackendManager


def _patch_forward(monkeypatch):
    import aiohttp

    calls = []

    class FakeResponse:
        status = 200

        async def json(self, content_type=None):
            return {
                "id": "chatcmpl-scribe",
                "object": "chat.completion",
                "choices": [{"message": {"role": "assistant", "content": "[notes] ok"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            }

        async def text(self):
            return "ok"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class FakeSession:
        def post(self, *args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *args, **kwargs: FakeSession())
    return calls


def _forward_url(call):
    return str(call["args"][0] if call["args"] else call["kwargs"].get("url", ""))


# --- M0: config only, green on main today ---------------------------------


def test_m0_pinned_scribe_harness_forwards_to_the_scribe_slot(tmp_path, monkeypatch):
    client, _db, manager_cls = _client(tmp_path, monkeypatch)
    calls = _patch_forward(monkeypatch)

    models = client.get("/harnesses/a0_scribe/v1/models")
    resp = client.post(SCRIBE_URL, headers=SCRIBE_HEADERS, json=_scribe_body())

    assert models.status_code == 200 and "scribe-model" in models.text
    assert resp.status_code == 200
    assert resp.headers.get("x-a0-request-id")
    assert [":8090/" in _forward_url(call) for call in calls] == [True]
    manager_cls._instance = None


def test_m0_scribe_requests_are_attributed_without_content(tmp_path, monkeypatch):
    import sqlite3

    client, db_path, manager_cls = _client(tmp_path, monkeypatch)
    _patch_forward(monkeypatch)

    assert client.post(SCRIBE_URL, headers=SCRIBE_HEADERS, json=_scribe_body()).status_code == 200

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT agent_id, agent_type, priority, status FROM requests"
        ).fetchone()
    assert row == ("a0_scribe", "scribe", "low", "completed")
    assert CONTENT_CANARY.encode() not in db_path.read_bytes()
    assert CONTENT_CANARY not in client.get("/routing/analytics").text
    manager_cls._instance = None


def test_m0_unhealthy_scribe_slot_fails_closed_instead_of_using_chat(tmp_path, monkeypatch):
    client, _db, manager_cls = _client(tmp_path, monkeypatch, scribe_healthy=False)
    calls = _patch_forward(monkeypatch)

    resp = client.post(SCRIBE_URL, headers=SCRIBE_HEADERS, json=_scribe_body())

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "harness_model_unavailable"
    assert calls == []  # silence beats a note written by the wrong model
    manager_cls._instance = None


# --- M1: dedicated admission lane, RED until implemented -------------------


def test_m1_scribe_work_never_waits_on_the_chat_lane(tmp_path, monkeypatch):
    client, _db, manager_cls = _client(
        tmp_path,
        monkeypatch,
        queue=FleetQueue(max_active=0, max_queue=0),  # shared local lane is full
        scribe_queue=FleetQueue(max_active=1, max_queue=4),
    )
    calls = _patch_forward(monkeypatch)

    pinned = client.post(SCRIBE_URL, headers=SCRIBE_HEADERS, json=_scribe_body())
    routed = client.post(
        "/v1/chat/completions",
        headers=SCRIBE_HEADERS,
        json={**_scribe_body(), "model": "scribe"},
    )
    chat = client.post(
        "/v1/chat/completions",
        json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert pinned.status_code == 200
    assert pinned.headers["x-a0-admission-lane"] == "local:scribe"
    assert routed.status_code == 200
    assert routed.headers["x-a0-admission-lane"] == "local:scribe"
    assert chat.status_code == 429  # the full chat lane is unchanged
    assert len(calls) == 2
    manager_cls._instance = None


def test_m1_full_scribe_lane_sheds_scribe_load_only(tmp_path, monkeypatch):
    client, _db, manager_cls = _client(
        tmp_path, monkeypatch, scribe_queue=FleetQueue(max_active=0, max_queue=0)
    )
    _patch_forward(monkeypatch)

    scribe = client.post(SCRIBE_URL, headers=SCRIBE_HEADERS, json=_scribe_body())
    chat = client.post(
        "/v1/chat/completions",
        json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert scribe.status_code == 429
    assert scribe.json()["error"]["code"] == "queue_full"
    assert chat.status_code == 200
    manager_cls._instance = None


def test_m1_scribe_lane_defaults_come_from_env_and_show_in_status(tmp_path, monkeypatch):
    monkeypatch.setenv("A0_FLEET_SCRIBE_MAX_ACTIVE", "2")
    monkeypatch.setenv("A0_FLEET_SCRIBE_MAX_QUEUE", "3")
    client, _db, manager_cls = _client(tmp_path, monkeypatch)

    lane = client.get("/fleet/status").json()["queues"]["local:scribe"]

    assert lane["mode"] == "bounded"
    assert (lane["max_active"], lane["max_queue"]) == (2, 3)
    manager_cls._instance = None


@pytest.mark.parametrize("active,waiting,expected", [
    (None, None, (1, 4)), ("bad", "bad", (1, 4)), ("-2", "-3", (1, 0)),
])
def test_scribe_limits_use_existing_environment_validation(tmp_path, monkeypatch, active, waiting, expected):
    for key, value in (("A0_FLEET_SCRIBE_MAX_ACTIVE", active), ("A0_FLEET_SCRIBE_MAX_QUEUE", waiting)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    client, _, _ = _client(tmp_path, monkeypatch)
    lane = client.get("/fleet/status").json()["queues"]["local:scribe"]
    assert (lane["max_active"], lane["max_queue"]) == expected


def test_agent_headers_cannot_select_scribe_lane(tmp_path, monkeypatch):
    client, _, _ = _client(tmp_path, monkeypatch, scribe_queue=FleetQueue(0, 0))
    _patch_forward(monkeypatch)
    response = client.post("/v1/chat/completions", headers=SCRIBE_HEADERS,
                           json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200
    assert response.headers["x-a0-admission-lane"] == "local"


def test_generic_scribe_fallback_uses_actual_chat_lane(tmp_path, monkeypatch):
    client, _, _ = _client(tmp_path, monkeypatch, scribe_healthy=False, scribe_queue=FleetQueue(0, 0))
    calls = _patch_forward(monkeypatch)
    response = client.post("/v1/chat/completions", headers=SCRIBE_HEADERS,
                           json={**_scribe_body(), "model": "scribe"})
    assert response.status_code == 200
    assert response.headers["x-a0-admission-lane"] == "local"
    assert ":8080/" in _forward_url(calls[0])


def test_chat_completes_while_scribe_is_still_forwarding(tmp_path, monkeypatch):
    import aiohttp

    lane = FleetQueue(1, 4)
    client, _, _ = _client(tmp_path, monkeypatch, scribe_queue=lane)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class Reply:
            status = 200

            def __init__(self, scribe):
                self.scribe = scribe

            async def json(self, **_kwargs):
                if self.scribe:
                    entered.set()
                    await release.wait()
                return {"choices": [{"message": {"content": "ok"}}]}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        class Session:
            def post(self, url, **_kwargs):
                return Reply(":8090/" in url)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        monkeypatch.setattr(aiohttp, "ClientSession", lambda **_kwargs: Session())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as http:
            pending = asyncio.create_task(http.post(SCRIBE_URL, json=_scribe_body()))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                assert lane.snapshot()["active"] == 1
                chat = await asyncio.wait_for(http.post("/v1/chat/completions", json={
                    "model": "chat", "messages": [{"role": "user", "content": "hi"}],
                }), 2)
                assert chat.status_code == 200
                assert not pending.done()
            finally:
                release.set()
                await pending
        assert lane.snapshot()["active"] == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize("privacy", [
    {"routing": {"local_only": True}}, {"local_only": True},
    {"metadata": {"local_only": True}}, {"routing": {"privacy_mode": "local_only"}},
    {"privacy_mode": "local_only"}, {"metadata": {"privacy_mode": "local_only"}},
])
def test_explicit_upstream_cannot_bypass_local_only(tmp_path, monkeypatch, pinned, privacy):
    import sys
    import sqlite3

    _write(tmp_path / "missing-upstreams.yaml", """
        upstreams:
          - name: external
            type: openai_compatible
            base_url: http://localhost:9999/v1
            enabled: true
            max_active: 1
            max_queue: 0
    """)
    monkeypatch.setattr(sys.modules[__name__], "_HARNESSES", _HARNESSES.replace("scribe-model", "external/utility"))
    client, db_path, _ = _client(tmp_path, monkeypatch)
    calls = _patch_forward(monkeypatch)
    response = client.post(SCRIBE_URL if pinned else "/v1/chat/completions", json={
        "model": "external/utility", "messages": [{"role": "user", "content": CONTENT_CANARY}], **privacy,
    })
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "local_only_violation"
    assert response.headers["x-a0-request-id"]
    assert response.headers["x-a0-admission-lane"] == "upstream:external"
    assert not calls
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT status, error_code FROM requests").fetchall() == [("rejected", "local_only_violation")]
        assert conn.execute("SELECT count(*) FROM queue_events").fetchone()[0] == 0
    assert CONTENT_CANARY.encode() not in db_path.read_bytes()


def test_local_pin_preserves_typed_privacy_and_cannot_escape(tmp_path, monkeypatch):
    client, _, _ = _client(tmp_path, monkeypatch)
    calls = _patch_forward(monkeypatch)
    response = client.post(SCRIBE_URL, json={**_scribe_body(), "routing": {
        "role": "chat", "preferred_slot": "chat", "local_only": "not-a-bool",
    }})
    assert response.status_code == 422
    assert not calls
    response = client.post(SCRIBE_URL, json={**_scribe_body(), "routing": {
        "role": "chat", "preferred_slot": "chat", "local_only": True,
    }})
    assert response.status_code == 200
    assert ":8090/" in _forward_url(calls[0])


def test_scribe_stream_holds_and_releases_its_queue(tmp_path, monkeypatch):
    import aiohttp

    lane = FleetQueue(1, 1)
    client, _, _ = _client(tmp_path, monkeypatch, scribe_queue=lane)
    active = []

    class Content:
        async def iter_chunked(self, _size):
            active.append(lane.snapshot()["active"])
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            yield b'data: [DONE]\n\n'

    class Reply:
        status = 200
        content = Content()

        def release(self):
            pass

    class Session:
        async def post(self, *_args, **_kwargs):
            return Reply()

        async def close(self):
            pass

    monkeypatch.setattr(aiohttp, "ClientSession", lambda **_kwargs: Session())
    response = client.post(SCRIBE_URL, json={**_scribe_body(), "stream": True})
    assert response.status_code == 200
    assert response.headers["x-a0-admission-lane"] == "local:scribe"
    assert active == [1]
    assert lane.snapshot()["active"] == 0


def test_scribe_queue_cancellation_preserves_other_active_request(tmp_path, monkeypatch):
    lane = FleetQueue(1, 1)
    client, _, _ = _client(tmp_path, monkeypatch, scribe_queue=lane)
    _patch_forward(monkeypatch)

    async def scenario():
        await lane.acquire("low")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as http:
            pending = asyncio.create_task(http.post(SCRIBE_URL, json=_scribe_body()))
            async def queued():
                while lane.snapshot()["queued"] != 1:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(queued(), 2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert lane.snapshot()["active"] == 1
        assert lane.snapshot()["queued"] == 0
        await lane.release()
        assert lane.snapshot()["active"] == 0

    asyncio.run(scenario())


def test_scribe_admission_telemetry_failure_does_not_leak_queue(tmp_path, monkeypatch):
    lane = FleetQueue(1, 1)
    client, _, _ = _client(tmp_path, monkeypatch, scribe_queue=lane)
    calls = _patch_forward(monkeypatch)

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic storage failure")

    monkeypatch.setattr(FleetStore, "update_request", fail)
    response = client.post(SCRIBE_URL, json=_scribe_body())
    assert response.status_code == 500
    assert lane.snapshot()["active"] == 0
    assert not calls

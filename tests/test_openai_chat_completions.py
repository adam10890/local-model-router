"""
Tests for the standalone OpenAI-compatible chat endpoint.

No real llama.cpp servers are required. Health probing and upstream forwarding
are stubbed independently.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "local_model_router"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from starlette.testclient import TestClient  # noqa: E402


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
  - id: embedding
    port: 8082
    host: localhost
    role: embed
    enabled: true
    model_id: embedding-model
    context_size: 8192
global:
  backend: remote
"""


def _client(tmp_path, monkeypatch, health_ok=True):
    from local_model_router.service.app import create_app
    from local_model_router.helpers.llama_cpp_manager import BackendManager

    BackendManager._instance = None
    cfg = tmp_path / "llama_cpp_servers.yaml"
    cfg.write_text(_ROUTING_CONFIG, encoding="utf-8")

    async def health_probe(url, timeout):
        return {"ok": health_ok}

    monkeypatch.setattr(
        "local_model_router.helpers.smart_router.health._aiohttp_probe",
        health_probe,
    )
    return TestClient(create_app(str(cfg))), BackendManager


def _patch_forward(monkeypatch, status=200, payload=None):
    import aiohttp

    calls = []
    response_payload = payload or {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
    }

    class FakeResponse:
        def __init__(self):
            self.status = status

        async def json(self, content_type=None):
            return response_payload

        async def text(self):
            return "not-json"

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

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: FakeSession())
    return calls


def _patch_stream_forward(monkeypatch, status=200, chunks=None):
    import aiohttp

    calls = []
    response_chunks = chunks or [
        b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    class FakeContent:
        async def iter_chunked(self, size):
            for chunk in response_chunks:
                yield chunk

    class FakeResponse:
        def __init__(self):
            self.status = status
            self.content = FakeContent()

        async def json(self, content_type=None):
            return {"error": {"message": "upstream failed"}}

        async def text(self):
            return "upstream failed"

        def release(self):
            return None

    class FakeSession:
        async def post(self, *args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return FakeResponse()

        async def close(self):
            return None

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *args, **kwargs: FakeSession())
    return calls


def test_chat_completions_forwards_to_selected_chat_slot(tmp_path, monkeypatch):
    client, manager_cls = _client(tmp_path, monkeypatch, health_ok=True)
    calls = _patch_forward(monkeypatch)

    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "local-chat",
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": 0.2,
        },
    )

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "ok"
    assert resp.headers["x-a0-router-slot-id"] == "chat"
    assert resp.headers["x-a0-admission-lane"] == "local"
    assert resp.headers["x-a0-request-id"].startswith("req_")
    assert resp.headers["x-hard-ctx"] == "65536"
    assert calls[0]["args"][0] == "http://localhost:8080/v1/chat/completions"
    assert calls[0]["kwargs"]["json"]["stream"] is False
    # "local-chat" is not a router alias — explicit model ids pass through
    # verbatim so Router Mode fleets can serve the exact requested model.
    assert calls[0]["kwargs"]["json"]["model"] == "local-chat"
    assert calls[0]["kwargs"]["json"]["messages"][0]["content"] == "hello"
    manager_cls._instance = None


def test_embeddings_forwards_through_router_selected_slot(tmp_path, monkeypatch):
    client, manager_cls = _client(tmp_path, monkeypatch, health_ok=True)
    calls = _patch_forward(
        monkeypatch,
        payload={"object": "list", "data": [{"embedding": [0.1, 0.2], "index": 0}]},
    )

    response = client.post(
        "/v1/embeddings",
        json={"model": "embedding", "input": ["hello"]},
    )

    assert response.status_code == 200
    assert response.json()["data"][0]["embedding"] == [0.1, 0.2]
    assert calls[0]["args"][0] == "http://localhost:8082/v1/embeddings"
    assert calls[0]["kwargs"]["json"] == {
        "model": "embedding-model",
        "input": ["hello"],
    }
    manager_cls._instance = None


def test_chat_completions_alias_model_uses_routing_decision(tmp_path, monkeypatch):
    client, manager_cls = _client(tmp_path, monkeypatch, health_ok=True)
    calls = _patch_forward(monkeypatch)

    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert resp.status_code == 200
    assert resp.headers["x-a0-router-slot-id"] == "chat"
    # recognized aliases forward whatever model the routing decision picked
    assert calls[0]["kwargs"]["json"]["model"] == "chat-model"
    manager_cls._instance = None


def test_chat_completions_coder_alias_routes_to_utility(tmp_path, monkeypatch):
    client, manager_cls = _client(tmp_path, monkeypatch, health_ok=True)
    calls = _patch_forward(monkeypatch)

    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "coder",
            "messages": [{"role": "user", "content": "write a function"}],
        },
    )

    assert resp.status_code == 200
    assert resp.headers["x-a0-router-slot-id"] == "utility"
    assert calls[0]["args"][0] == "http://localhost:8088/v1/chat/completions"
    assert calls[0]["kwargs"]["json"]["model"] == "utility-model"
    manager_cls._instance = None


def test_chat_completions_routing_metadata_can_prefer_utility_slot(tmp_path, monkeypatch):
    client, manager_cls = _client(tmp_path, monkeypatch, health_ok=True)
    calls = _patch_forward(monkeypatch)

    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "local-utility",
            "messages": [{"role": "user", "content": "summarize"}],
            "routing": {"preferred_slot": "utility", "role": "utility"},
        },
    )

    assert resp.status_code == 200
    assert resp.headers["x-a0-router-slot-id"] == "utility"
    assert resp.headers["x-hard-ctx"] == "32768"
    assert calls[0]["args"][0] == "http://localhost:8088/v1/chat/completions"
    # explicit non-alias model id passes through verbatim
    assert calls[0]["kwargs"]["json"]["model"] == "local-utility"
    assert "routing" not in calls[0]["kwargs"]["json"]
    manager_cls._instance = None


def test_chat_completions_task_type_subagent_uses_utility_slot(tmp_path, monkeypatch):
    client, manager_cls = _client(tmp_path, monkeypatch, health_ok=True)
    calls = _patch_forward(monkeypatch)

    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "run worker"}],
            "routing": {"task_type": "sub_agent_task"},
        },
    )

    assert resp.status_code == 200
    assert resp.headers["x-a0-router-slot-id"] == "utility"
    assert calls[0]["args"][0] == "http://localhost:8088/v1/chat/completions"
    assert calls[0]["kwargs"]["json"]["model"] == "utility-model"
    manager_cls._instance = None


def test_chat_completions_streams_selected_slot_sse(tmp_path, monkeypatch):
    client, manager_cls = _client(tmp_path, monkeypatch, health_ok=True)
    calls = _patch_stream_forward(monkeypatch)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    ) as resp:
        body = resp.read()

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-a0-router-slot-id"] == "chat"
    assert calls[0]["args"][0] == "http://localhost:8080/v1/chat/completions"
    assert calls[0]["kwargs"]["json"]["stream"] is True
    assert b'data: {"choices":[{"delta":{"content":"he"}}]}' in body
    assert b"data: [DONE]" in body
    manager_cls._instance = None


def test_incomplete_sse_stream_reports_upstream_stream_incomplete():
    import asyncio

    from local_model_router.service.app import _stream_upstream_response

    codes: list[str] = []

    class FakeContent:
        async def iter_chunked(self, size):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'

    class FakeResponse:
        content = FakeContent()

        def release(self):
            return None

    class FakeSession:
        async def close(self):
            return None

    async def on_error(code: str) -> None:
        codes.append(code)

    async def drain():
        chunks = []
        async for chunk in _stream_upstream_response(FakeResponse(), FakeSession(), on_error):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(drain())
    assert chunks
    assert codes == ["upstream_stream_incomplete"]


def test_chat_completions_requires_bearer_token_when_api_key_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("A0_LMM_ROUTER_API_KEY", "local-secret")
    client, manager_cls = _client(tmp_path, monkeypatch, health_ok=True)

    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "unauthorized"
    assert "local-secret" not in str(body)
    manager_cls._instance = None


def test_chat_completions_accepts_valid_bearer_token_when_api_key_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("A0_LMM_ROUTER_API_KEY", "local-secret")
    client, manager_cls = _client(tmp_path, monkeypatch, health_ok=True)
    calls = _patch_forward(monkeypatch)

    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer local-secret"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 200
    assert calls[0]["args"][0] == "http://localhost:8080/v1/chat/completions"
    manager_cls._instance = None


def test_chat_completions_returns_503_when_no_slot_available(tmp_path, monkeypatch):
    client, manager_cls = _client(tmp_path, monkeypatch, health_ok=False)

    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "no_slot_available"
    assert body["routing"]["no_slot_available"] is True
    manager_cls._instance = None

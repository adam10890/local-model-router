"""Tests for passive bounded SSE usage extraction and streaming accounting."""
from __future__ import annotations

import sqlite3

import aiohttp
from starlette.testclient import TestClient

from local_model_router.service.app import create_app
from local_model_router.service.fleet_manager import FleetQueue, FleetStore
from local_model_router.service.context import _STREAM_TIMEOUT
from local_model_router.service.sse_usage import (
    SseUsageExtractor,
    UsageOnlyEventFilter,
    request_stream_usage,
)


def test_stream_timeout_bounds_silence_not_total_length():
    # A long local generation must not be cut at the forward timeout.
    assert _STREAM_TIMEOUT.total is None
    assert _STREAM_TIMEOUT.sock_read and _STREAM_TIMEOUT.sock_connect


def test_request_stream_usage_respects_client_choice():
    injected = {"stream": True}
    assert request_stream_usage(injected) is True
    assert injected["stream_options"] == {"include_usage": True}

    own = {"stream": True, "stream_options": {"include_usage": True}}
    assert request_stream_usage(own) is False
    assert request_stream_usage({"stream": False}) is False


def test_usage_only_event_filter_drops_split_usage_event_and_keeps_the_rest():
    filt = UsageOnlyEventFilter()
    out = filt.feed(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: {"choices":[],"us')
    out += filt.feed(b'age":{"prompt_tokens":1}}\r\n\r\ndata: {"error":{"code":"x"}}\n\ndata: [DONE]')
    out += filt.flush()
    assert b'"usage"' not in out
    assert b'"content":"hi"' in out
    assert b'"error"' in out
    assert out.endswith(b"data: [DONE]")


def test_sse_usage_extractor_handles_split_chunks_crlf_and_bounds():
    extractor = SseUsageExtractor(max_line_bytes=512)
    oversized = b"data: " + (b"x" * 700) + b"\n"
    chunk1 = (
        oversized
        + b'data: {"choices":[{"delta":{"content":"hello "}}]}\r\n'
        + b'data: {"choices":[],"usage":{"prompt_to'
    )
    chunk2 = (
        b'kens":14,"completion_tokens":9}}\n'
        + b"data: [DONE]\n"
    )
    extractor.feed(chunk1)
    extractor.feed(chunk2)
    extractor.finish()

    assert extractor.usage() == {"prompt_tokens": 14, "completion_tokens": 9}
    assert extractor._buffer == bytearray()  # noqa: SLF001


def test_sse_usage_extractor_ignores_booleans_and_partial_non_usage_without_body_retention():
    extractor = SseUsageExtractor()
    extractor.feed(b'data: {"choices":[{"delta":{"content":"secret prompt text"}}],"usage":{"prompt_tokens":true}}\n')
    extractor.feed(b"data: not-json\n")
    extractor.finish()

    assert extractor.usage() == {}


def test_local_streaming_injects_stream_options_and_persists_usage(tmp_path, monkeypatch):
    conf = tmp_path / "llama_cpp_servers.yaml"
    conf.write_text(
        """active_slots:
  - id: slot_chat
    host: 127.0.0.1
    port: 18080
    role: chat
    enabled: true
global:
  backend: remote
""",
        encoding="utf-8",
    )
    (tmp_path / "apps.yaml").write_text("apps: {}\n", encoding="utf-8")
    (tmp_path / "harnesses.yaml").write_text("harnesses: {}\n", encoding="utf-8")
    (tmp_path / "upstreams.yaml").write_text("upstreams: {}\n", encoding="utf-8")

    async def healthy(_url, _timeout):
        return {"ok": True}

    monkeypatch.setattr("local_model_router.helpers.smart_router.health._aiohttp_probe", healthy)

    sent_payloads: list[dict] = []

    class FakeContent:
        async def iter_chunked(self, _n):
            yield b'data: {"id":"1","choices":[{"delta":{"content":"hi"}}]}\n\n'
            yield b'data: {"id":"1","choices":[],"usage":{"prompt_tokens":21,"completion_tokens":7}}\n\ndata: [DONE]\n\n'

    class FakeResponse:
        status = 200
        content = FakeContent()

        def release(self):
            pass

    class FakeSession:
        async def post(self, _url, json=None, headers=None):
            sent_payloads.append(dict(json or {}))
            return FakeResponse()

        async def close(self):
            pass

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *_a, **_kw: FakeSession())

    db_path = tmp_path / "fleet.sqlite3"
    store = FleetStore(str(db_path))
    app = create_app(
        str(conf),
        fleet_store=store,
        fleet_queue=FleetQueue(),
        setup_home=str(tmp_path / "setup"),
    )
    request_body = {
        "model": "chat",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    with TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json=request_body)
        assert resp.status_code == 200
        assert "data: [DONE]" in resp.text
        # The router asked for usage, so the client must not see that chunk.
        assert '"usage"' not in resp.text
        assert '"content":"hi"' in resp.text

    assert "stream_options" not in request_body
    assert sent_payloads[0]["stream_options"] == {"include_usage": True}

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, prompt_tokens, completion_tokens FROM requests LIMIT 1"
        ).fetchone()
    assert row == ("completed", 21, 7)


def test_upstream_streaming_records_sse_usage_into_store_and_ledger(tmp_path, monkeypatch):
    conf = tmp_path / "llama_cpp_servers.yaml"
    conf.write_text("global:\n  backend: remote\nactive_slots: []\n", encoding="utf-8")
    (tmp_path / "apps.yaml").write_text("apps: {}\n", encoding="utf-8")
    (tmp_path / "harnesses.yaml").write_text("harnesses: {}\n", encoding="utf-8")
    (tmp_path / "upstreams.yaml").write_text(
        """upstreams:
  - name: cloudbox
    type: openai_compatible
    base_url: http://localhost:11434/v1
    enabled: true
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("A0_LMM_ROUTER_ENABLED_UPSTREAMS", "cloudbox")

    sent_payloads: list[dict] = []

    class FakeContent:
        async def iter_chunked(self, _n):
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            yield b'data: {"usage":{"prompt_tokens":33,"completion_tokens":12}}\n\ndata: [DONE]\n\n'

    class FakeResponse:
        status = 200
        content = FakeContent()

        def release(self):
            pass

    class FakeSession:
        async def post(self, _url, json=None, headers=None):
            sent_payloads.append(dict(json or {}))
            return FakeResponse()

        async def close(self):
            pass

    monkeypatch.setattr(aiohttp, "ClientSession", lambda *_a, **_kw: FakeSession())

    db_path = tmp_path / "fleet.sqlite3"
    store = FleetStore(str(db_path))
    app = create_app(
        str(conf),
        fleet_store=store,
        fleet_queue=FleetQueue(),
        upstreams_path=str(tmp_path / "upstreams.yaml"),
        setup_home=str(tmp_path / "setup"),
    )
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "cloudbox/demo-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        assert '"usage"' not in resp.text
        assert "data: [DONE]" in resp.text

        # A client that asks for usage itself still receives it.
        own = client.post(
            "/v1/chat/completions",
            json={
                "model": "cloudbox/demo-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
        assert '"usage"' in own.text

    # Upstreams are asked for usage so streamed cloud spend reaches the ledger.
    assert sent_payloads[0]["stream_options"] == {"include_usage": True}

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, prompt_tokens, completion_tokens FROM requests LIMIT 1"
        ).fetchone()
    assert row == ("completed", 33, 12)

    from local_model_router.helpers import usage_ledger

    events = usage_ledger.all_events()
    assert len(events) == 2  # hidden-usage request + client-requested usage
    assert events[0]["provider_id"] == "cloudbox"
    assert events[0]["tokens_in"] == 33
    assert events[0]["tokens_out"] == 12
    assert usage_ledger.window_totals("cloudbox", 3600) == {"tokens": 90, "requests": 2}

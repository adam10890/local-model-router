"""Tests for upstream backend adapters: registry, listing, and forwarding."""
from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

import pytest
from starlette.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_model_router.service.app import create_app  # noqa: E402
from local_model_router.upstreams.registry import (  # noqa: E402
    load_upstreams,
    match_upstream_model,
    parse_window,
)

_FLEET_CONFIG = textwrap.dedent("""
    active_slots:
      - id: slot_router
        host: localhost
        port: 8080
        role: chat
        enabled: true
        router_mode: true
    global:
      backend: remote
""")

_UPSTREAMS = textwrap.dedent("""
    upstreams:
      - name: ollama
        type: openai_compatible
        base_url: http://localhost:11434/v1
        enabled: true
      - name: vllm
        type: openai_compatible
        base_url: http://localhost:8000/v1
        enabled: false
      - name: airllm
        type: airllm
        enabled: true
        experimental: true
      - name: badtype
        type: quantum
        enabled: true
""")


def _write(tmp_path, name, content):
    target = tmp_path / name
    target.write_text(content, encoding="utf-8")
    return str(target)


def _make_app(tmp_path, monkeypatch, fetch=None):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    return create_app(
        _write(tmp_path, "llama_cpp_servers.yaml", _FLEET_CONFIG),
        models_fetch=fetch,
        upstreams_path=_write(tmp_path, "upstreams.yaml", _UPSTREAMS),
        apps_path=str(tmp_path / "missing-apps.yaml"),
    )


# ---------------------------------------------------------------------------
# registry parsing
# ---------------------------------------------------------------------------

def test_load_upstreams_parses_and_filters(tmp_path):
    upstreams = load_upstreams(_write(tmp_path, "upstreams.yaml", _UPSTREAMS))
    names = [u.name for u in upstreams]
    assert names == ["ollama", "vllm"]  # non-serving and unknown types dropped

    ollama = upstreams[0]
    assert ollama.serves_inference is True
    assert ollama.describe()["auth_configured"] is False

    vllm = upstreams[1]
    assert vllm.serves_inference is False  # disabled



def test_load_upstreams_missing_file_yields_empty(tmp_path):
    assert load_upstreams(tmp_path / "nope.yaml") == []


def test_api_key_comes_from_env_only(tmp_path):
    content = textwrap.dedent("""
        upstreams:
          - name: secured
            type: openai_compatible
            base_url: http://localhost:9999/v1
            api_key_env: SECURED_KEY
            enabled: true
    """)
    upstream = load_upstreams(_write(tmp_path, "upstreams.yaml", content))[0]
    assert upstream.api_key(env={}) == ""
    assert upstream.api_key(env={"SECURED_KEY": "abc"}) == "abc"
    assert upstream.headers(env={"SECURED_KEY": "abc"})["Authorization"] == "Bearer abc"
    assert "Authorization" not in upstream.headers(env={})
    assert "abc" not in str(upstream.describe())


def test_match_upstream_model(tmp_path):
    upstreams = load_upstreams(_write(tmp_path, "upstreams.yaml", _UPSTREAMS))
    match = match_upstream_model("ollama/llama3.3:70b", upstreams)
    assert match is not None
    assert match[0].name == "ollama"
    assert match[1] == "llama3.3:70b"

    assert match_upstream_model("vllm/some-model", upstreams) is None  # disabled
    assert match_upstream_model("airllm/huge-model", upstreams) is None
    assert match_upstream_model("chat", upstreams) is None
    assert match_upstream_model(None, upstreams) is None


# ---------------------------------------------------------------------------
# declared usage limits
# ---------------------------------------------------------------------------

def test_parse_window():
    assert parse_window("5h") == 5 * 3600
    assert parse_window("7d") == 7 * 86400
    for bad in ("", "5", "h", "5m", "-5h", "5hh", "five"):
        with pytest.raises(ValueError):
            parse_window(bad)


def test_limits_parse_into_limit_window_tuple(tmp_path):
    content = textwrap.dedent("""
        upstreams:
          - name: capped
            type: openai_compatible
            base_url: http://localhost:9999/v1
            enabled: true
            limits:
              - window: "5h"
                max_tokens: 1000000
              - window: "7d"
                max_requests: 5000
    """)
    upstream = load_upstreams(_write(tmp_path, "upstreams.yaml", content))[0]
    assert upstream.has_declared_limits is True
    assert len(upstream.limits) == 2
    assert upstream.limits[0].window == "5h"
    assert upstream.limits[0].max_tokens == 1000000
    assert upstream.limits[0].max_requests is None
    assert upstream.limits[1].window == "7d"
    assert upstream.limits[1].max_requests == 5000


def test_describe_surfaces_limits(tmp_path):
    content = textwrap.dedent("""
        upstreams:
          - name: capped
            type: openai_compatible
            base_url: http://localhost:9999/v1
            enabled: true
            limits:
              - window: "5h"
                max_tokens: 1000000
    """)
    upstream = load_upstreams(_write(tmp_path, "upstreams.yaml", content))[0]
    desc = upstream.describe()
    assert desc["has_declared_limits"] is True
    assert desc["limits"] == [{"window": "5h", "max_tokens": 1000000, "max_requests": None}]

    no_limits = load_upstreams(_write(tmp_path, "upstreams2.yaml", _UPSTREAMS))[0]
    assert no_limits.describe()["has_declared_limits"] is False
    assert no_limits.describe()["limits"] == []


def test_subscription_type_has_no_inference_but_carries_limits(tmp_path):
    content = textwrap.dedent("""
        upstreams:
          - name: codex
            type: subscription
            invoke: codex_cli
            default_model: gpt-5-codex
            enabled: true
            limits:
              - window: "5h"
                max_tokens: 1000000
              - window: "7d"
                max_requests: 5000
    """)
    upstream = load_upstreams(_write(tmp_path, "upstreams.yaml", content))[0]
    assert upstream.type == "subscription"
    assert upstream.base_url == ""
    assert upstream.serves_inference is False  # no base_url, ever
    assert upstream.invoke == "codex_cli"
    assert upstream.default_model == "gpt-5-codex"
    assert upstream.has_declared_limits is True
    assert len(upstream.limits) == 2


def test_malformed_limit_degrades_gracefully(tmp_path):
    content = textwrap.dedent("""
        upstreams:
          - name: capped
            type: openai_compatible
            base_url: http://localhost:9999/v1
            enabled: true
            limits:
              - window: "5h"
                max_tokens: 1000000
              - window: "not-a-window"
                max_tokens: 999
              - not_a_dict_entry
    """)
    upstream = load_upstreams(_write(tmp_path, "upstreams.yaml", content))[0]
    # the malformed and non-dict entries are skipped; the valid one survives
    assert len(upstream.limits) == 1
    assert upstream.limits[0].window == "5h"


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

def test_backends_endpoint_lists_fleet_and_upstreams(tmp_path, monkeypatch):
    client = TestClient(_make_app(tmp_path, monkeypatch))
    resp = client.get("/backends")
    assert resp.status_code == 200
    payload = resp.json()["backends"]
    by_name = {entry["name"]: entry for entry in payload}
    assert by_name["local_fleet"]["type"] == "llama_cpp_fleet"
    assert by_name["local_fleet"]["slots"][0]["id"] == "slot_router"
    assert by_name["ollama"]["serves_inference"] is True
    assert "airllm" not in by_name


def test_v1_models_includes_namespaced_upstream_models(tmp_path, monkeypatch):
    async def fetch(base_url: str):
        if "11434" in base_url:
            return [{"id": "llama3.3:70b"}, {"id": "qwen2.5-coder:32b"}]
        return [{"id": "chat", "meta": {"n_ctx": 131072}}]

    client = TestClient(_make_app(tmp_path, monkeypatch, fetch=fetch))
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    by_id = {row["id"]: row for row in resp.json()["data"]}
    ids = set(by_id)
    assert "ollama/llama3.3:70b" in ids
    assert "ollama/qwen2.5-coder:32b" in ids
    assert by_id["ollama/llama3.3:70b"]["source"] == "upstream"
    assert by_id["ollama/llama3.3:70b"]["capabilities"]["json_mode"] is True
    # disabled vllm and non-serving airllm contribute nothing
    assert not any(i.startswith("vllm/") or i.startswith("airllm/") for i in ids)


def test_v1_models_probes_serving_upstreams_concurrently(tmp_path, monkeypatch):
    active = 0
    max_active = 0

    async def fetch(base_url: str):
        nonlocal active, max_active
        if base_url.endswith(":8080/v1"):
            return []
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return []

    upstreams = _UPSTREAMS.replace("enabled: false", "enabled: true", 1)
    client = TestClient(create_app(
        _write(tmp_path, "llama_cpp_servers.yaml", _FLEET_CONFIG),
        models_fetch=fetch,
        upstreams_path=_write(tmp_path, "upstreams.yaml", upstreams),
        apps_path=str(tmp_path / "missing-apps.yaml"),
    ))

    assert client.get("/v1/models").status_code == 200
    assert max_active == 2


def test_chat_completions_forwards_to_matching_upstream(tmp_path, monkeypatch):
    import aiohttp

    calls = []

    class FakeResponse:
        status = 200

        async def json(self, content_type=None):
            return {"choices": [{"message": {"content": "from-ollama"}}], "model": "llama3.3:70b"}

        def release(self):
            return None

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        def post(self, url, **kwargs):
            calls.append({"url": url, "kwargs": kwargs})

            class _Ctx:
                async def __aenter__(_self):
                    return FakeResponse()

                async def __aexit__(_self, *exc):
                    return None

            return _Ctx()

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    client = TestClient(_make_app(tmp_path, monkeypatch))

    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "ollama/llama3.3:70b",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "from-ollama"
    assert resp.headers["x-a0-router-upstream"] == "ollama"
    assert calls[0]["url"] == "http://localhost:11434/v1/chat/completions"
    assert calls[0]["kwargs"]["json"]["model"] == "llama3.3:70b"  # prefix stripped

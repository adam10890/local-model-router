from __future__ import annotations

import textwrap

from starlette.testclient import TestClient


_CONFIG = textwrap.dedent(
    """
    active_slots:
      - id: chat
        port: 8080
        host: localhost
        role: chat
        enabled: true
        model_id: chat-model
        context_size: 8192
        supports_tools: false
        supports_json_mode: true
        quality_hint: 0.9
        latency_hint_ms: 180
      - id: utility
        port: 8088
        host: localhost
        role: utility
        enabled: true
        model_id: utility-model
        context_size: 32768
        supports_tools: true
        supports_json_mode: true
        quality_hint: 0.6
        latency_hint_ms: 35
    global:
      backend: remote
      failover_chains:
        chat: [chat, utility]
        utility: [utility, chat]
    """
)


def _make_client(tmp_path, monkeypatch, *, cache_enabled=False):
    from local_model_router.helpers.llama_cpp_manager import BackendManager
    from local_model_router.service.app import create_app
    from local_model_router.service.fleet_manager import FleetQueue, FleetStore

    BackendManager._instance = None
    cfg = tmp_path / "llama_cpp_servers.yaml"
    cfg.write_text(_CONFIG, encoding="utf-8")
    monkeypatch.setenv("A0_LMM_ROUTER_PROMPT_CACHE", "1" if cache_enabled else "0")

    async def health_probe(url, timeout):
        return {"ok": True}

    monkeypatch.setattr(
        "local_model_router.helpers.smart_router.health._aiohttp_probe",
        health_probe,
    )
    store = FleetStore(":memory:")
    queue = FleetQueue(max_active=1, max_queue=4)
    return TestClient(create_app(str(cfg), fleet_store=store, fleet_queue=queue)), store, BackendManager


def _patch_forward(monkeypatch):
    import aiohttp

    calls = []

    class FakeResponse:
        status = 200

        async def json(self, content_type=None):
            return {
                "id": "chatcmpl-local",
                "object": "chat.completion",
                "model": "utility-model",
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
                "choices": [{"message": {"role": "assistant", "content": "cached answer"}}],
            }

        async def text(self):
            return "not-json"

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def post(self, url, **kwargs):
            calls.append({"url": url, "kwargs": kwargs})

            class _Ctx:
                async def __aenter__(_self):
                    return FakeResponse()

                async def __aexit__(_self, *args):
                    return None

            return _Ctx()

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    return calls


def test_chat_auto_uses_capability_aware_routing_headers_and_telemetry(tmp_path, monkeypatch):
    client, store, manager_cls = _make_client(tmp_path, monkeypatch)
    calls = _patch_forward(monkeypatch)

    resp = client.post(
        "/v1/chat/completions",
        headers={"X-App-Id": "aider"},
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": "call a tool"}],
            "tools": [{"type": "function", "function": {"name": "lookup"}}],
            "routing": {"routing_strategy": "fastest", "estimated_tokens": 12000},
        },
    )

    assert resp.status_code == 200
    assert resp.headers["x-a0-router-requested-model"] == "auto"
    assert resp.headers["x-a0-router-resolved-model"] == "utility-model"
    assert resp.headers["x-a0-router-strategy"] == "fastest"
    assert resp.headers["x-a0-router-cache"] == "BYPASS"
    assert resp.headers["x-a0-router-slot-id"] == "utility"
    assert calls[0]["url"] == "http://localhost:8088/v1/chat/completions"

    analytics = client.get("/routing/analytics").json()
    assert analytics["recent"][0]["requested_model"] == "auto"
    assert analytics["recent"][0]["resolved_model"] == "utility-model"
    assert analytics["recent"][0]["app_id"] == "aider"
    assert "call a tool" not in str(analytics)
    manager_cls._instance = None


def test_prompt_cache_is_opt_in_and_avoids_second_upstream_call(tmp_path, monkeypatch):
    client, _store, manager_cls = _make_client(tmp_path, monkeypatch, cache_enabled=True)
    calls = _patch_forward(monkeypatch)
    payload = {
        "model": "auto",
        "messages": [{"role": "user", "content": "repeatable"}],
        "temperature": 0,
    }

    first = client.post("/v1/chat/completions", json=payload)
    second = client.post("/v1/chat/completions", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.headers["x-a0-router-cache"] == "MISS"
    assert second.headers["x-a0-router-cache"] == "HIT"
    assert len(calls) == 1
    manager_cls._instance = None


def test_model_catalog_and_card_endpoints_expose_safe_discovery(tmp_path, monkeypatch):
    client, _store, manager_cls = _make_client(tmp_path, monkeypatch)

    catalog = client.get("/routing/models").json()
    ids = {item["id"] for item in catalog["models"]}
    assert {"chat", "utility"} <= ids

    card = client.get("/routing/models/utility-model").json()
    assert card["model"]["model_id"] == "utility-model"
    assert card["model"]["capabilities"]["tools"] is True
    assert "api_key" not in str(card).lower()

    auto_card = client.get("/routing/models/auto").json()
    assert auto_card["model"]["capabilities"]["auto_route"] is True
    manager_cls._instance = None


def test_dashboard_exposes_compare_routing_and_integration_snippets():
    from local_model_router.dashboard import dashboard_html

    html = dashboard_html()
    assert "v0.9.0" in html
    assert "__IMPERIUM_VERSION__" not in html
    assert "Routing" in html
    assert "Claude Code MCP" in html
    assert "Dify" in html
    assert "Vercel AI SDK" in html
    assert "Pinned model" in html
    assert "Connect an agent" not in html


def test_routing_evaluation_endpoint_and_decision_use_latest_snapshot(tmp_path, monkeypatch):
    client, store, manager_cls = _make_client(tmp_path, monkeypatch)
    store.record_model_snapshot("model_evaluation", {
        "schema_version": 1,
        "generated_at": "2026-07-17T00:00:00Z",
        "models": [{
            "model_id": "utility-model",
            "fingerprint": "safe-hash",
            "roles": {"utility": {
                "pass_rate": 0.95,
                "reliability": 1.0,
                "median_latency_ms": 20,
                "resource_cost_hint": 0.3,
            }},
        }],
    })

    snapshot = client.get("/routing/evaluations")
    decision = client.post("/routing/request", json={
        "agent_id": "test",
        "agent_type": "custom",
        "task_type": "coding",
        "routing_strategy": "quality",
    })

    assert snapshot.status_code == 200
    assert snapshot.json()["payload"]["models"][0]["fingerprint"] == "safe-hash"
    assert decision.status_code == 200
    assert "evaluated_model_score" in decision.json()["reason_codes"]
    assert "prompt" not in str(snapshot.json()).lower()
    manager_cls._instance = None

from __future__ import annotations

import asyncio


def test_mcp_bridge_uses_router_url_and_api_key(monkeypatch):
    from local_model_router.mcp import router_bridge

    monkeypatch.setenv("A0_LMM_ROUTER_BASE_URL", "http://router.test:9100/")
    monkeypatch.setenv("A0_LMM_ROUTER_API_KEY", "secret")

    assert router_bridge._router_base_url() == "http://router.test:9100"
    assert router_bridge._router_headers()["Authorization"] == "Bearer secret"
    assert not hasattr(router_bridge, "_get_manager")


def test_mcp_bridge_routes_chat_through_openai_endpoint(monkeypatch):
    from local_model_router.mcp import router_bridge

    calls = []

    async def fake_request(method, path, payload=None, timeout=30):
        calls.append((method, path, payload, timeout))
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(router_bridge, "_router_request", fake_request)
    result = asyncio.run(
        router_bridge.chat_complete([{"role": "user", "content": "hi"}], role="utility")
    )

    assert result["choices"][0]["message"]["content"] == "ok"
    assert calls[0][0:2] == ("POST", "/v1/chat/completions")
    assert calls[0][2]["model"] == "utility"
    assert calls[0][2]["routing"]["role"] == "utility"


def test_mcp_bridge_maps_discovery_and_lifecycle_to_router(monkeypatch):
    from local_model_router.mcp import router_bridge

    calls = []

    async def fake_request(method, path, payload=None, timeout=30):
        calls.append((method, path, payload))
        return {"ok": True, "models": []}

    monkeypatch.setattr(router_bridge, "_router_request", fake_request)

    asyncio.run(router_bridge.list_models())
    asyncio.run(router_bridge.model_card("vendor/model"))
    asyncio.run(router_bridge.fleet_status())
    asyncio.run(router_bridge.start_slot("chat slot"))
    asyncio.run(router_bridge.stop_slot("chat slot"))
    asyncio.run(router_bridge.start_fleet())

    assert calls == [
        ("GET", "/routing/models", None),
        ("GET", "/routing/models/vendor%2Fmodel", None),
        ("GET", "/fleet/status", None),
        ("POST", "/fleet/slots/chat%20slot/start", {}),
        ("POST", "/fleet/slots/chat%20slot/stop", {}),
        ("POST", "/fleet/start", {}),
    ]

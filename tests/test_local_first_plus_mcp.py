from __future__ import annotations

import asyncio


def test_mcp_bridge_discovers_models_via_fleet_manager(monkeypatch):
    from local_model_router.mcp import router_bridge

    async def fake_get(path):
        if path == "/routing/models":
            return {"models": [{"id": "chat", "model_id": "chat-model"}]}
        raise AssertionError(path)

    monkeypatch.setattr(router_bridge, "_fleet_manager_get", fake_get)

    result = asyncio.run(router_bridge.list_models())

    assert result["models"][0]["id"] == "chat"


def test_mcp_bridge_model_card_uses_fleet_manager(monkeypatch):
    from local_model_router.mcp import router_bridge

    async def fake_get(path):
        if path == "/routing/models/chat-model":
            return {"model": {"id": "chat", "model_id": "chat-model"}}
        raise AssertionError(path)

    monkeypatch.setattr(router_bridge, "_fleet_manager_get", fake_get)

    result = asyncio.run(router_bridge.model_card("chat-model"))

    assert result["model"]["model_id"] == "chat-model"


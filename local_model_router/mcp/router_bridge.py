"""HTTP bridge from MCP tools to the running local model router."""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import aiohttp


def _router_base_url() -> str:
    return os.environ.get("A0_LMM_ROUTER_BASE_URL", "http://127.0.0.1:9000").strip().rstrip("/")


def _router_headers() -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "X-Agent-ID": os.environ.get("A0_MCP_AGENT_ID", "mcp-router"),
        "X-Agent-Type": "mcp",
        "X-App-Id": "mcp",
        "X-Priority": os.environ.get("A0_MCP_PRIORITY", "normal"),
    }
    api_key = os.environ.get("A0_LMM_ROUTER_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def _router_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method,
                f"{_router_base_url()}{path}",
                json=payload,
                headers=_router_headers(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                data = await response.json(content_type=None)
                if isinstance(data, dict):
                    if response.status >= 400:
                        data.setdefault("status", response.status)
                    return data
                return {"data": data, "status": response.status}
    except (aiohttp.ClientError, TimeoutError) as exc:
        return {"error": "router_unreachable", "detail": str(exc)}


async def chat_complete(
    messages: list[dict[str, str]],
    role: str = "chat",
    max_tokens: int = 2048,
    temperature: float = 0.7,
    stream: bool = False,
) -> dict[str, Any]:
    del stream
    return await _router_request(
        "POST",
        "/v1/chat/completions",
        {
            "model": role,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
            "routing": {"role": role, "agent_type": "mcp"},
        },
        timeout=120,
    )


async def get_embeddings(texts: list[str]) -> dict[str, Any]:
    return await _router_request(
        "POST",
        "/v1/embeddings",
        {"input": texts, "model": "embedding"},
        timeout=60,
    )


async def fleet_status() -> dict[str, Any]:
    return await _router_request("GET", "/fleet/status")


async def list_models() -> dict[str, Any]:
    return await _router_request("GET", "/routing/models")


async def model_card(model_id: str) -> dict[str, Any]:
    safe_id = str(model_id or "").strip()
    if not safe_id:
        return {"error": "model_id_required"}
    return await _router_request("GET", f"/routing/models/{quote(safe_id, safe='')}")


async def providers_list() -> dict[str, Any]:
    return await _router_request("GET", "/backends")


async def compute_budget() -> dict[str, Any]:
    return await _router_request("GET", "/compute/budget")


async def route_preview(
    role: str = "chat",
    task_type: str = "chat",
    requires_tools: bool = False,
    requires_vision: bool = False,
    requires_json_mode: bool = False,
    estimated_tokens: int | None = None,
    routing_strategy: str = "balanced_local",
    local_only: bool = False,
) -> dict[str, Any]:
    return await _router_request(
        "POST",
        "/routing/request",
        {
            "agent_id": "mcp-router",
            "agent_type": "mcp",
            "role": role,
            "task_type": task_type,
            "requires_tools": requires_tools,
            "requires_vision": requires_vision,
            "requires_json_mode": requires_json_mode,
            "estimated_tokens": estimated_tokens,
            "routing_strategy": routing_strategy,
            "local_only": local_only,
        },
    )


async def start_slot(slot_id: str) -> dict[str, Any]:
    return await _router_request(
        "POST", f"/fleet/slots/{quote(slot_id, safe='')}/start", {}
    )


async def stop_slot(slot_id: str) -> dict[str, Any]:
    return await _router_request(
        "POST", f"/fleet/slots/{quote(slot_id, safe='')}/stop", {}
    )


async def start_fleet() -> dict[str, Any]:
    return await _router_request("POST", "/fleet/start", {}, timeout=120)


async def slot_configs() -> dict[str, dict[str, Any]]:
    slots = await _router_request("GET", "/slots")
    rows = slots.get("data", slots) if isinstance(slots, dict) else []
    if isinstance(rows, list):
        return {str(row.get("id") or row.get("name")): row for row in rows if isinstance(row, dict)}
    return {}


async def hardware_profile() -> dict[str, Any]:
    status = await fleet_status()
    return status.get("compute") or {}

"""
router_bridge.py — Bridge between MCP tool calls and the BackendManager.

Uses the existing BackendManager singleton (llama_cpp_manager.py) for
slot lifecycle and failover, and aiohttp for proxying HTTP requests to
the appropriate llama.cpp container.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

import aiohttp

logger = logging.getLogger("lmm_router.mcp.bridge")

# Allow running outside the /a0 container for testing.


def _get_manager():
    """Return the BackendManager singleton."""
    from local_model_router.helpers.llama_cpp_manager import BackendManager  # noqa: PLC0415
    return BackendManager.get_instance()


def _fleet_manager_base_url() -> str:
    return os.environ.get("A0_FLEET_MANAGER_BASE_URL", "").strip().rstrip("/")


def _fleet_manager_headers() -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "X-Agent-ID": os.environ.get("A0_MCP_AGENT_ID", "mcp-router"),
        "X-Agent-Type": "mcp",
        "X-Priority": os.environ.get("A0_MCP_PRIORITY", "normal"),
    }
    api_key = os.environ.get("A0_FLEET_MANAGER_API_KEY", os.environ.get("A0_LMM_ROUTER_API_KEY", "")).strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def _fleet_manager_get(path: str) -> dict[str, Any] | None:
    base_url = _fleet_manager_base_url()
    if not base_url:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{base_url}{path}",
                headers=_fleet_manager_headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400 and isinstance(data, dict):
                    return {"error": data.get("error", data), "status": resp.status}
                return data
    except aiohttp.ClientError as exc:
        return {"error": str(exc)}


async def _fleet_manager_post(path: str, payload: dict[str, Any], timeout: int = 120) -> dict[str, Any] | None:
    base_url = _fleet_manager_base_url()
    if not base_url:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base_url}{path}",
                json=payload,
                headers=_fleet_manager_headers(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400 and isinstance(data, dict):
                    return {"error": data.get("error", data), "status": resp.status}
                return data
    except aiohttp.ClientError as exc:
        return {"error": str(exc)}


def _slot_url(role: str, fallback_port_map: dict[str, int] | None = None) -> str | None:
    """Return the base v1 URL for a slot by role, using failover if needed.

    Synchronous. Used by get_embeddings and any non-async callers.
    chat_complete uses select_slot_with_failover_async instead.
    """
    mgr = _get_manager()

    # select_slot_with_failover returns a decision dict with 'url'
    decision = mgr.select_slot_with_failover(role)
    if decision:
        url = decision.get("url", "")
        if url:
            return url

    return _config_fallback_url(role, fallback_port_map)


def _config_fallback_url(
    role: str, fallback_port_map: dict[str, int] | None = None
) -> str | None:
    """Return URL for role from lmm_hosts config or static port map.

    Does not perform any health probe. Used as a last resort when
    slot selection returns no healthy slot.
    """
    mgr = _get_manager()
    hosts: dict[str, str] = mgr.global_config.get("lmm_hosts", {})
    if role in hosts:
        return f"http://{hosts[role]}/v1"

    defaults = fallback_port_map or {"chat": 8080, "utility": 8088, "embedding": 8082, "scribe": 8090}
    port = defaults.get(role)
    return f"http://localhost:{port}/v1" if port else None


async def chat_complete(
    messages: list[dict[str, str]],
    role: str = "chat",
    max_tokens: int = 2048,
    temperature: float = 0.7,
    stream: bool = False,
) -> dict[str, Any]:
    """Forward a chat completion request to the appropriate slot.

    Uses the async routing path so health probes do not block the event loop.
    The routing decision is computed once; slot_id is reused in error paths.
    """
    fleet_payload = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
        "routing": {"role": role, "agent_type": "mcp"},
    }
    fleet_result = await _fleet_manager_post("/v1/chat/completions", fleet_payload)
    if fleet_result is not None:
        return fleet_result

    mgr = _get_manager()
    decision = await mgr.select_slot_with_failover_async(role)

    if decision:
        url = decision.get("url", "")
        slot_id = decision.get("slot_id", f"slot_{role}")
    else:
        url = _config_fallback_url(role) or ""
        slot_id = f"slot_{role}"

    if not url:
        return {"error": f"No healthy slot found for role '{role}'"}

    payload: dict[str, Any] = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{url}/chat/completions",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    mgr.mark_slot_error(slot_id, f"HTTP {resp.status}")
                return data
    except aiohttp.ClientError as exc:
        mgr.mark_slot_error(slot_id, str(exc))
        return {"error": str(exc)}


async def get_embeddings(texts: list[str]) -> dict[str, Any]:
    """Forward an embedding request to slot_embedding."""
    url = _slot_url("embedding")
    if not url:
        return {"error": "No healthy embedding slot found"}

    payload = {"input": texts, "model": "local-embed"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{url}/embeddings",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                return await resp.json()
    except aiohttp.ClientError as exc:
        return {"error": str(exc)}


async def fleet_status() -> dict[str, Any]:
    """Return current status of all slots."""
    fleet_result = await _fleet_manager_get("/fleet/status")
    if fleet_result is not None:
        return fleet_result

    mgr = _get_manager()
    try:
        slots = await mgr.status()
    except Exception as exc:
        slots = {}
        logger.warning("fleet_status async failed: %s", exc)

    failover_info = {}
    try:
        failover_info = mgr.get_failover_status()
    except Exception:
        pass

    return {
        "slots": slots,
        "failover": failover_info,
        "backend": mgr.backend_type,
    }


async def start_slot(slot_id: str) -> dict[str, Any]:
    if _fleet_manager_base_url():
        return {
            "ok": False,
            "error": "Fleet Manager V1 is Docker-socket-free; start_slot belongs to a future fleet-node worker.",
            "slot_id": slot_id,
        }
    return await _get_manager().start_slot(slot_id)


async def stop_slot(slot_id: str) -> bool:
    if _fleet_manager_base_url():
        return False
    return await _get_manager().stop_slot(slot_id)


async def start_fleet() -> dict[str, Any]:
    if _fleet_manager_base_url():
        return {
            "ok": False,
            "error": "Fleet Manager V1 is Docker-socket-free; start_fleet belongs to a future fleet-node worker.",
        }
    return await _get_manager().start_all()


def slot_configs() -> dict[str, Any]:
    """Return raw slot configs (for resource introspection)."""
    mgr = _get_manager()
    return getattr(mgr, "_slot_configs", {})


def hardware_profile() -> dict[str, Any]:
    """Return hardware info from config."""
    mgr = _get_manager()
    try:
        import yaml  # noqa: PLC0415
        with open(mgr.config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("hardware", {})
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _decision_slot_id(role: str) -> str:
    """Get the slot id used for the given role (for error marking)."""
    mgr = _get_manager()
    decision = mgr.select_slot_with_failover(role)
    return decision.get("slot_id", f"slot_{role}") if decision else f"slot_{role}"

"""
resources.py — MCP resource definitions for lmm-router.

Resources expose read-only data that MCP clients can surface to users
or inject into model context. Registered via register_resources().
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from local_model_router.mcp import router_bridge as bridge


def register_resources(mcp: FastMCP) -> None:
    """Register all router resources onto the MCP server instance."""

    @mcp.resource("models://fleet/status")
    async def fleet_status_resource() -> str:
        """Live snapshot of all llama.cpp slots — running state, ports, model ids."""
        data = await bridge.fleet_status()
        return json.dumps(data, indent=2)

    @mcp.resource("models://{slot_id}/info")
    async def slot_info_resource(slot_id: str) -> str:
        """Configuration info for a specific slot (does not require it to be running).

        slot_id: e.g. slot_chat, slot_utility, slot_embedding
        Returns: role, port, model_id, context_size, enabled flag.
        """
        configs = await bridge.slot_configs()
        cfg = configs.get(slot_id)
        if cfg is None:
            return json.dumps({"error": f"Slot '{slot_id}' not found"})
        return json.dumps({
            "slot_id": slot_id,
            "role": cfg.get("role", ""),
            "port": cfg.get("port", ""),
            "model_id": cfg.get("model_id", ""),
            "context_size": cfg.get("context_size", ""),
            "enabled": cfg.get("enabled", True),
            "host": cfg.get("host", "localhost"),
        }, indent=2)

    @mcp.resource("models://hardware/profile")
    async def hardware_profile_resource() -> str:
        """Hardware inventory declared in llama_cpp_servers.yaml.

        Includes GPUs (VRAM, CUDA cores), CPUs, RAM, and model concurrency limits.
        """
        data = await bridge.hardware_profile()
        return json.dumps(data, indent=2)

    @mcp.resource("models://slots/list")
    async def slots_list_resource() -> str:
        """Flat list of all configured slots with role and model_id.

        Useful for clients that want to enumerate available models without
        knowing slot ids in advance.
        """
        configs = await bridge.slot_configs()
        slots = [
            {
                "id": name,
                "role": cfg.get("role", ""),
                "model_id": cfg.get("model_id", ""),
                "port": cfg.get("port", ""),
                "enabled": cfg.get("enabled", True),
            }
            for name, cfg in configs.items()
        ]
        return json.dumps({"slots": slots}, indent=2)

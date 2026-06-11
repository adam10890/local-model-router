"""
tools.py — MCP tool definitions for lmm-router.

All tools are registered onto a FastMCP instance via register_tools().
Tools cover: LLM inference (chat, utility, embedding, smart-route),
fleet management (status, start/stop, assign model), and model discovery.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from local_model_router.mcp import router_bridge as bridge


def register_tools(mcp: FastMCP, allow_mutating_tools: bool = False) -> None:
    """Register all router tools onto the MCP server instance."""

    # ── Inference tools ────────────────────────────────────────────────────

    @mcp.tool()
    async def chat_completion(
        messages: list[dict],
        max_tokens: int = 2048,
        temperature: float = 0.7,
        system_prompt: str = "",
    ) -> dict:
        """Send a chat completion request to the local chat model (slot_chat).

        Falls back to slot_utility if slot_chat is unhealthy.
        messages: list of {"role": "user"|"assistant"|"system", "content": "..."}
        """
        if system_prompt:
            messages = [{"role": "system", "content": system_prompt}] + list(messages)
        return await bridge.chat_complete(
            messages=messages,
            role="chat",
            max_tokens=max_tokens,
            temperature=temperature,
        )

    @mcp.tool()
    async def utility_completion(
        messages: list[dict],
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> dict:
        """Send a completion request to the utility model (slot_utility).

        Optimised for short, fast, tool-calling responses.
        Falls back to slot_chat if slot_utility is unhealthy.
        """
        return await bridge.chat_complete(
            messages=messages,
            role="utility",
            max_tokens=max_tokens,
            temperature=temperature,
        )

    @mcp.tool()
    async def route_completion(
        messages: list[dict],
        role: str = "chat",
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> dict:
        """Route a completion request to the best available slot for the given role.

        role: "chat" | "utility" | "embedding"
        The router automatically applies failover chains defined in llama_cpp_servers.yaml.
        """
        if role == "embedding":
            texts = [m.get("content", "") for m in messages if m.get("content")]
            return await bridge.get_embeddings(texts)
        return await bridge.chat_complete(
            messages=messages,
            role=role,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    @mcp.tool()
    async def get_embeddings(texts: list[str]) -> dict:
        """Generate text embeddings using the local embedding model (slot_embedding).

        texts: list of strings to embed.
        Returns an OpenAI-compatible embeddings response.
        """
        return await bridge.get_embeddings(texts)

    # ── Fleet management tools ─────────────────────────────────────────────

    @mcp.tool()
    async def fleet_status() -> dict:
        """Return the live status of all llama.cpp slots and the failover state.

        Includes: running/healthy flags, ports, model_id, container_id per slot,
        plus failover chain configuration and error-slot list.
        """
        return await bridge.fleet_status()

    if allow_mutating_tools:
        @mcp.tool()
        async def start_fleet() -> dict:
            """Start all configured llama.cpp slots in parallel.

            Returns per-slot start results. Uses the backend configured in
            llama_cpp_servers.yaml (remote | docker | subprocess | auto).
            """
            return await bridge.start_fleet()

        @mcp.tool()
        async def start_slot(slot_id: str) -> dict:
            """Start a single llama.cpp slot by its id (e.g. 'slot_chat').

            Returns running/healthy status and any error message.
            """
            return await bridge.start_slot(slot_id)

        @mcp.tool()
        async def stop_slot(slot_id: str) -> dict:
            """Stop a single llama.cpp slot by its id.

            Returns True on success, False on failure.
            """
            ok = await bridge.stop_slot(slot_id)
            return {"stopped": ok, "slot_id": slot_id}

    # ── Model discovery ────────────────────────────────────────────────────

    @mcp.tool()
    def list_slots() -> dict:
        """List all configured slots with their role, port, and model_id.

        Does not require slots to be running.
        """
        configs = bridge.slot_configs()
        result = {}
        for name, cfg in configs.items():
            result[name] = {
                "role": cfg.get("role", ""),
                "port": cfg.get("port", ""),
                "model_id": cfg.get("model_id", ""),
                "enabled": cfg.get("enabled", True),
            }
        return result

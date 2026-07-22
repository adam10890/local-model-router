"""
tools.py — MCP tool definitions for lmm-router.

All tools are registered onto a FastMCP instance via register_tools().
Tools cover: LLM inference (chat, utility, embedding, smart-route),
fleet management (status, start/stop, assign model), and model discovery.
"""

from __future__ import annotations

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
            return await bridge.stop_slot(slot_id)

    # ── Model discovery ────────────────────────────────────────────────────

    @mcp.tool()
    async def list_slots() -> dict:
        """List all configured slots with their role, port, and model_id.

        Does not require slots to be running.
        """
        configs = await bridge.slot_configs()
        result = {}
        for name, cfg in configs.items():
            result[name] = {
                "role": cfg.get("role", ""),
                "port": cfg.get("port", ""),
                "model_id": cfg.get("model_id", ""),
                "enabled": cfg.get("enabled", True),
            }
        return result

    @mcp.tool()
    async def list_models(
        capability: str = "",
        source: str = "",
        min_context: int = 0,
    ) -> dict:
        """List model catalog entries with optional local filtering.

        capability: optional "tools" | "vision" | "json_mode".
        source: optional "local_fleet" | "upstream".
        min_context: optional minimum context window.
        """
        catalog = await bridge.list_models()
        models = list(catalog.get("models", []))
        if capability:
            models = [m for m in models if (m.get("capabilities") or {}).get(capability)]
        if source:
            models = [m for m in models if m.get("source") == source]
        if min_context:
            models = [m for m in models if int(m.get("context_size") or 0) >= min_context]
        return {"models": models, "count": len(models)}

    @mcp.tool()
    async def model_card(model_id: str) -> dict:
        """Return safe model details: role, source, capabilities, context, and hints."""
        return await bridge.model_card(model_id)

    @mcp.tool()
    async def providers_list() -> dict:
        """List local fleet and configured upstream providers."""
        return await bridge.providers_list()

    @mcp.tool()
    async def route_preview(
        role: str = "chat",
        task_type: str = "chat",
        requires_tools: bool = False,
        requires_vision: bool = False,
        requires_json_mode: bool = False,
        estimated_tokens: int | None = None,
        routing_strategy: str = "balanced_local",
        local_only: bool = False,
    ) -> dict:
        """Preview which local model would serve a task without forwarding a prompt."""
        return await bridge.route_preview(
            role=role,
            task_type=task_type,
            requires_tools=requires_tools,
            requires_vision=requires_vision,
            requires_json_mode=requires_json_mode,
            estimated_tokens=estimated_tokens,
            routing_strategy=routing_strategy,
            local_only=local_only,
        )

    @mcp.tool()
    async def compute_budget() -> dict:
        """Return the live compute-budget state for every provider — local hardware capacity
        plus, per subscription/upstream, rolling-window usage vs declared limits (or live
        Codex usage). Recommend calling before planning heavy or parallel work.
        """
        return await bridge.compute_budget()

    @mcp.tool()
    async def route_task(
        task: str = "",
        role: str = "chat",
        est_input_tokens: int = 0,
        est_output_tokens: int = 0,
        quality: str = "best_available",
    ) -> dict:
        """Recommend-only routing packet for a task: like route_preview, this never forwards
        the prompt — it reports which local slot or upstream model WOULD serve the task and
        why, and the calling agent still makes the actual call itself. Unlike route_preview,
        the recommendation now factors live per-provider compute budgets: a provider whose
        subscription/rate-limit window is exhausted is excluded from selection, and one
        running low is kept but flagged, both visible in the response's reason_codes/warnings
        and budget block. Pass est_input_tokens/est_output_tokens for a token estimate and
        quality ("best_available" | "fast" | ...) to bias selection.
        """
        return await bridge.route_task(
            task=task,
            role=role,
            est_input_tokens=est_input_tokens,
            est_output_tokens=est_output_tokens,
            quality=quality,
        )

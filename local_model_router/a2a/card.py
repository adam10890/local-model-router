"""A2A agent card and skill handlers.

The router publishes itself as an agent specializing in local model
selection: other agents discover it via the agent card at
``/.well-known/agent-card.json`` and call skills at ``POST /a2a``.

Surface division (architectural rule):
  - OpenAI-compatible API → model clients
  - MCP → tool-using agents
  - A2A → agent-to-agent collaboration

Skills are thin wrappers over the same routing/observer logic the HTTP
API uses — no duplicate policy. The card never exposes secrets, raw
logs, or backend credentials.
"""
from __future__ import annotations

from typing import Any, Dict

from local_model_router import __version__

AGENT_NAME = "Local Model Router Agent"
AGENT_DESCRIPTION = (
    "An agent that manages local LLM backends, selects the best local model "
    "for a task, explains routing decisions, monitors model health, and "
    "coordinates model-serving resources for other agents."
)
PROTOCOL_VERSION = "0.3.0"

SKILLS = [
    {
        "id": "route_llm_task",
        "name": "Route an LLM task",
        "description": (
            "Given a task description and constraints (task type, privacy "
            "policy, context estimate), return the selected local model, "
            "slot, backend, and an explainable decision with reason codes."
        ),
        "tags": ["routing", "model-selection", "local-first"],
        "input_modes": ["application/json"],
        "output_modes": ["application/json"],
    },
    {
        "id": "check_backend_health",
        "name": "Check backend health",
        "description": "Return health for the local fleet slots and configured upstream backends.",
        "tags": ["health", "monitoring"],
        "input_modes": ["application/json"],
        "output_modes": ["application/json"],
    },
    {
        "id": "list_models",
        "name": "List available models",
        "description": "List router aliases and live local models with context sizes.",
        "tags": ["models", "discovery"],
        "input_modes": ["application/json"],
        "output_modes": ["application/json"],
    },
]

CAPABILITIES = [
    "model_routing",
    "model_recommendation",
    "local_inference_gateway",
    "backend_health_monitoring",
    "routing_explanation",
    "local_only_policy_enforcement",
    "fallback_planning",
]


def agent_card(base_url: str) -> Dict[str, Any]:
    """Build the public agent card. ``base_url`` is the router's own URL."""
    base = base_url.rstrip("/")
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": AGENT_NAME,
        "description": AGENT_DESCRIPTION,
        "url": f"{base}/a2a",
        "preferredTransport": "JSONRPC",
        "version": __version__,
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": SKILLS,
        "metadata": {
            "agent_capabilities": CAPABILITIES,
            "surfaces": {
                "openai_compatible": f"{base}/v1",
                "mcp": "streamable-http (see documentation)",
                "a2a": f"{base}/a2a",
            },
            "local_only": True,
        },
    }


def skill_ids() -> set[str]:
    return {skill["id"] for skill in SKILLS}

"""Model alias resolution for the OpenAI-compatible surface.

Clients send ``model`` values like ``auto``, ``fast``, ``coder``, or a real
router alias (``chat``, ``utility``, ``embedding``, ``scribe``). This module
maps them onto fleet roles so the routing engine can pick a slot, without
each client needing to know the fleet layout.

Pure module: no I/O, no config reads. The service layer decides what to do
with unrecognized names (they fall through as explicit upstream model ids).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

AUTO = "auto"

# Public, stable alias surface. Values are fleet roles.
ALIAS_TO_ROLE = {
    # generalist / large-context
    "chat": "chat",
    "default": "chat",
    "deep": "chat",
    "long_context": "chat",
    # fast / small / worker
    "fast": "utility",
    "utility": "utility",
    "util": "utility",
    "fast_utility": "utility",
    # coding (served by the utility lane until a dedicated coder slot exists)
    "coder": "utility",
    "code": "utility",
    "coding": "utility",
    # embeddings
    "embedding": "embed",
    "embeddings": "embed",
    "embed": "embed",
    # background documentation model
    "scribe": "scribe",
}

# Task types that route to the fast/worker lane when model == "auto".
_AUTO_UTILITY_TASKS = frozenset({
    "background_worker", "coding", "debugging", "planning",
    "private_data_processing", "research", "sub_agent_task", "tool_calling",
    "classification", "summarization_short", "json",
})


@dataclass(frozen=True)
class AliasResolution:
    """Outcome of resolving a client-supplied model name."""

    requested: str
    role: Optional[str]      # fleet role, or None when not recognized
    recognized: bool         # True for aliases and AUTO
    is_auto: bool = False


def resolve_alias(model: Optional[str], task_type: str = "chat") -> AliasResolution:
    """Map a client ``model`` value to a fleet role.

    ``auto`` (or empty) infers the role from ``task_type``. Unrecognized
    names return ``recognized=False`` so callers can treat them as explicit
    model ids for a specific slot or upstream backend.
    """
    requested = str(model or "").strip()
    normalized = requested.lower()

    if not normalized or normalized == AUTO:
        task = str(task_type or "chat").strip().lower()
        if task in {"embedding", "embeddings"}:
            role = "embed"
        elif task in _AUTO_UTILITY_TASKS:
            role = "utility"
        else:
            role = "chat"
        return AliasResolution(requested=requested or AUTO, role=role, recognized=True, is_auto=True)

    role = ALIAS_TO_ROLE.get(normalized)
    if role is not None:
        return AliasResolution(requested=requested, role=role, recognized=True)

    return AliasResolution(requested=requested, role=None, recognized=False)


def public_aliases() -> dict[str, str]:
    """Stable alias→role map plus ``auto``, for /v1/models listings."""
    table = dict(ALIAS_TO_ROLE)
    table[AUTO] = "task-dependent"
    return table

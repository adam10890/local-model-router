"""
Agent Client Contract — routing intent request/response schema and policy handler.

POST /routing/request accepts a RoutingIntentRequest and returns a
RoutingDecisionResponse.  It is DRY-RUN ONLY: it calls the routing logic
to determine which slot would be selected, but never forwards the prompt
to any model.

Design principles:
  - Unknown agent_type / task_type values are accepted; they produce warnings
    rather than hard failures so new agents can integrate without schema updates.
  - Privacy flags are enforced as hard policy before routing.
  - Cloud routing is acknowledged but not implemented; the response says so
    explicitly rather than silently pretending.
  - All capability gaps (long context, tools, code) are surfaced as warnings.
  - response.dry_run is always True in this phase.
"""
from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from local_model_router.routing.catalog import (
    RoutingNeeds,
    build_slot_candidates,
    rank_candidates,
)

# ---------------------------------------------------------------------------
# Known value sets (not enums — unknown values are allowed with a warning)
# ---------------------------------------------------------------------------

_KNOWN_AGENT_TYPES = frozenset({
    "agent_zero", "hermes", "openclaw", "pi_coding_agent",
    "claude_code", "n8n", "custom", "unknown",
})

_KNOWN_TASK_TYPES = frozenset({
    "chat", "planning", "coding", "summarization", "embedding",
    "tool_calling", "private_data_processing", "sub_agent_task",
    "background_worker", "research", "debugging", "documentation",
})

_KNOWN_PRIVACY_MODES = frozenset({
    "local_only", "prefer_local", "cloud_allowed", "cloud_preferred", "unknown",
})

_KNOWN_PREFERENCES = frozenset({"fast", "normal", "quality"})
DRY_RUN_MODE = os.environ.get("A0_ROUTING_DRY_RUN", "1") != "0"

# Role inferred from task_type when role is not explicitly provided.
_TASK_TO_ROLE: Dict[str, str] = {
    "embedding":                "embed",
    "coding":                   "utility",
    "debugging":                "utility",
    "planning":                 "utility",
    "research":                 "utility",
    "tool_calling":             "utility",
    "private_data_processing":  "utility",
    "background_worker":        "utility",
    "sub_agent_task":           "utility",
    "documentation":            "scribe",
    "summarization":            "chat",
    "chat":                     "chat",
}


def _role_from_task_type(task_type: str) -> str:
    return _TASK_TO_ROLE.get(task_type.lower(), "chat")


def _router_alias_from_role(role: str) -> str:
    role_key = (role or "chat").lower()
    if role_key in {"embed", "embedding"}:
        return "embedding"
    if role_key == "utility":
        return "utility"
    if role_key == "scribe":
        return "scribe"
    return "chat"


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------

class RoutingIntentRequest(BaseModel):
    """Describes an agent's routing intent.  All fields are optional."""

    agent_id:   str = "unknown"
    agent_type: str = "unknown"

    role:      Optional[str] = None   # chat | utility | embed; inferred if absent
    task_type: str = "chat"

    privacy_mode:  str = "unknown"    # local_only | prefer_local | cloud_allowed | …
    local_only:    bool = False
    cloud_allowed: bool = True

    requires_long_context:    bool = False
    requires_tools:           bool = False
    requires_vision:          bool = False
    requires_json_mode:       bool = False
    requires_code_execution:  bool = False

    latency_preference: str = "normal"   # fast | normal | quality
    quality_preference: str = "normal"
    cost_preference:    str = "normal"
    routing_strategy:   str = "balanced_local"

    estimated_tokens: Optional[int] = None
    preferred_slot:   Optional[str] = None
    requested_model:   Optional[str] = None
    app_id:            Optional[str] = None

    input_classification: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("estimated_tokens")
    @classmethod
    def _positive_tokens(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 0:
            raise ValueError("estimated_tokens must be non-negative")
        return v


# ---------------------------------------------------------------------------
# Response schema
# ---------------------------------------------------------------------------

class RoutingDecisionResponse(BaseModel):
    """Dry-run routing decision.  Never contains secrets or model weights."""

    decision_id: str
    dry_run: bool = Field(
        default=True,
        description="True for dry-run routing decisions; set false only when this handler owns live forwarding.",
    )

    # Echo of key request fields
    agent_id:     str
    agent_type:   str
    role:         str
    task_type:    str
    privacy_mode: str

    # Selected slot (None when no_slot_available)
    selected_slot_id:      Optional[str]
    selected_url:          Optional[str]
    selected_backend_type: Optional[str]
    selected_model:        Optional[str]
    selected_source:       Optional[str] = None
    selected_candidate_id: Optional[str] = None
    routing_strategy:      str = "balanced_local"
    score:                 Optional[float] = None
    score_inputs:          Dict[str, Any] = Field(default_factory=dict)
    ranked_candidates:     List[Dict[str, Any]] = Field(default_factory=list)

    # Policy flags
    local_only_enforced: bool
    cloud_allowed:       bool
    no_slot_available:   bool
    fallback_used:       bool

    # Diagnostics
    reason_codes:    List[str]
    warnings:        List[str]
    health_snapshot: Optional[str]  # "healthy" | "unhealthy" | "unknown" | None


# ---------------------------------------------------------------------------
# Policy handler
# ---------------------------------------------------------------------------

class RoutingIntentHandler:
    """
    Applies the Agent Client Contract policy and returns a RoutingDecisionResponse.

    Never forwards the prompt.  Never mutates config or state.
    Uses the observer's _make_manager() seam so tests can inject stubs.
    """

    def __init__(self, observer: Any) -> None:
        self._observer = observer

    async def handle(self, req: RoutingIntentRequest) -> RoutingDecisionResponse:
        decision_id = str(uuid.uuid4())
        warnings: List[str] = []
        reason_codes: List[str] = []

        # ── 1. Validate known value sets (unknown → warning, not error) ──────
        if req.agent_type not in _KNOWN_AGENT_TYPES:
            warnings.append(f"unknown_agent_type:{req.agent_type}")
        if req.task_type not in _KNOWN_TASK_TYPES:
            warnings.append(f"unknown_task_type:{req.task_type}")
            reason_codes.append("unknown_task_type_fallback_to_chat")
        if req.privacy_mode not in _KNOWN_PRIVACY_MODES:
            warnings.append(f"unknown_privacy_mode:{req.privacy_mode}")
        for pref_name, pref_val in [
            ("latency_preference", req.latency_preference),
            ("quality_preference", req.quality_preference),
            ("cost_preference",    req.cost_preference),
        ]:
            if pref_val not in _KNOWN_PREFERENCES:
                warnings.append(f"unknown_{pref_name}:{pref_val}")

        # ── 2. Resolve role ────────────────────────────────────────────────
        role = req.role or _role_from_task_type(req.task_type)

        # ── 3. Privacy policy ──────────────────────────────────────────────
        local_only_enforced = req.local_only or req.privacy_mode == "local_only"
        # Effective cloud_allowed: False when local-only is enforced.
        effective_cloud_allowed = req.cloud_allowed and not local_only_enforced

        # Warn about cloud routing — this system is local llama.cpp only.
        if req.privacy_mode in ("cloud_preferred", "cloud_allowed") or (
            req.cloud_allowed and not local_only_enforced
        ):
            warnings.append(
                "cloud_routing_not_implemented: "
                "all routing is local llama.cpp only; "
                "cloud_allowed flag is noted but no cloud provider is configured"
            )

        # ── 4. Capability warnings ─────────────────────────────────────────
        if req.requires_long_context:
            warnings.append(
                "long_context_routing_applied: "
                "context-size-aware slot ranking is active when context metadata is available"
            )
        if req.requires_tools:
            warnings.append(
                "tool_routing_applied: "
                "tool-capability-aware ranking is active when slot metadata is available"
            )
        if req.requires_vision:
            warnings.append(
                "vision_routing_applied: "
                "vision-capability ranking is active when slot metadata is available"
            )
        if req.requires_json_mode:
            warnings.append(
                "json_mode_routing_applied: "
                "JSON-mode capability ranking is active when slot metadata is available"
            )
        if req.requires_code_execution:
            warnings.append(
                "code_execution_routing_not_implemented: "
                "code-execution-aware routing is not yet implemented"
            )

        # ── 5. Get manager ─────────────────────────────────────────────────
        try:
            mgr = self._observer._make_manager()
        except Exception as exc:
            return RoutingDecisionResponse(
                decision_id=decision_id,
                dry_run=DRY_RUN_MODE,
                agent_id=req.agent_id,
                agent_type=req.agent_type,
                role=role,
                task_type=req.task_type,
                privacy_mode=req.privacy_mode,
                selected_slot_id=None,
                selected_url=None,
                selected_backend_type=None,
                selected_model=None,
                selected_source=None,
                selected_candidate_id=None,
                routing_strategy=req.routing_strategy,
                score=None,
                score_inputs={},
                ranked_candidates=[],
                local_only_enforced=local_only_enforced,
                cloud_allowed=effective_cloud_allowed,
                no_slot_available=True,
                fallback_used=False,
                reason_codes=reason_codes + ["manager_init_failed"],
                warnings=warnings + [f"BackendManager init error: {type(exc).__name__}: {exc}"],
                health_snapshot=None,
            )

        # ── 6. Route ───────────────────────────────────────────────────────
        slot_rows: List[Dict[str, Any]] = []
        for slot_id, slot_cfg in getattr(mgr, "_slot_configs", {}).items():
            row = dict(slot_cfg)
            row.setdefault("id", slot_id)
            try:
                row["health"] = await mgr._get_slot_health_async(slot_id)
            except Exception:
                row["health"] = "unknown"
            slot_rows.append(row)

        needs = RoutingNeeds(
            role=role,
            task_type=req.task_type,
            requires_tools=req.requires_tools,
            requires_vision=req.requires_vision,
            requires_json_mode=req.requires_json_mode,
            requires_long_context=req.requires_long_context,
            estimated_tokens=req.estimated_tokens,
            local_only=local_only_enforced,
            strategy=req.routing_strategy,
            preferred_slot=req.preferred_slot,
        )
        ranked = rank_candidates(needs, build_slot_candidates(slot_rows), strategy=req.routing_strategy)
        ranked_public = [item.public_dict() for item in ranked[:5]]
        selected_rank = ranked[0] if ranked else None
        preferred_slot = req.preferred_slot
        route_role = role
        if selected_rank is not None:
            preferred_slot = selected_rank.candidate.slot_id or preferred_slot
            route_role = selected_rank.candidate.role or role
            reason_codes.extend(selected_rank.reason_codes)
            reason_codes.append(f"routing_strategy:{selected_rank.score_inputs.get('strategy', req.routing_strategy)}")
        else:
            reason_codes.append("no_candidate_satisfies_requirements")

        decision = await mgr.select_slot_with_failover_async(route_role, preferred_slot)

        if not decision:
            reason_codes.append("no_healthy_slot_in_chain")
            return RoutingDecisionResponse(
                decision_id=decision_id,
                dry_run=DRY_RUN_MODE,
                agent_id=req.agent_id,
                agent_type=req.agent_type,
                role=role,
                task_type=req.task_type,
                privacy_mode=req.privacy_mode,
                selected_slot_id=None,
                selected_url=None,
                selected_backend_type=None,
                selected_model=None,
                selected_source=selected_rank.candidate.source if selected_rank else None,
                selected_candidate_id=selected_rank.candidate.id if selected_rank else None,
                routing_strategy=req.routing_strategy,
                score=round(selected_rank.score, 4) if selected_rank else None,
                score_inputs=selected_rank.score_inputs if selected_rank else {},
                ranked_candidates=ranked_public,
                local_only_enforced=local_only_enforced,
                cloud_allowed=effective_cloud_allowed,
                no_slot_available=True,
                fallback_used=False,
                reason_codes=reason_codes,
                warnings=warnings,
                health_snapshot=None,
            )

        slot_id = decision.get("slot_id")
        slot_url = decision.get("url")
        fallback_used = decision.get("is_failover", False)
        if selected_rank is not None and selected_rank.candidate.slot_id and selected_rank.candidate.slot_id != slot_id:
            fallback_used = True

        # ── 7. Enrich from slot config ─────────────────────────────────────
        slot_cfg: Dict[str, Any] = mgr._slot_configs.get(slot_id, {})
        backend_type: str = mgr.backend_type
        if slot_cfg.get("router_mode"):
            model_id = slot_cfg.get("router_default_model") or _router_alias_from_role(role)
        else:
            model_id = slot_cfg.get("model_id") or slot_cfg.get("router_default_model")

        # ── 8. Health snapshot ─────────────────────────────────────────────
        try:
            health_snapshot = await mgr._get_slot_health_async(slot_id)
        except Exception:
            health_snapshot = "unknown"

        reason_codes.append("slot_selected")
        if fallback_used:
            reason_codes.append("primary_slot_unavailable_failover_used")

        return RoutingDecisionResponse(
            decision_id=decision_id,
            dry_run=DRY_RUN_MODE,
            agent_id=req.agent_id,
            agent_type=req.agent_type,
            role=role,
            task_type=req.task_type,
            privacy_mode=req.privacy_mode,
            selected_slot_id=slot_id,
            selected_url=slot_url,
            selected_backend_type=backend_type,
            selected_model=model_id,
            selected_source=selected_rank.candidate.source if selected_rank else "local_fleet",
            selected_candidate_id=selected_rank.candidate.id if selected_rank else slot_id,
            routing_strategy=req.routing_strategy,
            score=round(selected_rank.score, 4) if selected_rank else None,
            score_inputs=selected_rank.score_inputs if selected_rank else {},
            ranked_candidates=ranked_public,
            local_only_enforced=local_only_enforced,
            cloud_allowed=effective_cloud_allowed,
            no_slot_available=False,
            fallback_used=fallback_used,
            reason_codes=reason_codes,
            warnings=warnings,
            health_snapshot=health_snapshot,
        )

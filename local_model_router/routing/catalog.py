"""Local-first model catalog and scoring helpers.

Pure routing utilities used by HTTP, MCP, and tests. The module does not read
config files, touch the network, or know about Starlette.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .aliases import resolve_alias

VALID_STRATEGIES = {"balanced_local", "fastest", "quality", "economy"}
_TASK_TO_ROLE = {
    "embedding": "embed",
    "coding": "utility",
    "debugging": "utility",
    "planning": "utility",
    "research": "utility",
    "tool_calling": "utility",
    "private_data_processing": "utility",
    "background_worker": "utility",
    "sub_agent_task": "utility",
    "documentation": "scribe",
    "summarization": "chat",
    "chat": "chat",
}


@dataclass(frozen=True)
class RoutingNeeds:
    role: str = "chat"
    task_type: str = "chat"
    requires_tools: bool = False
    requires_vision: bool = False
    requires_json_mode: bool = False
    requires_long_context: bool = False
    estimated_tokens: Optional[int] = None
    local_only: bool = False
    strategy: str = "balanced_local"
    preferred_slot: Optional[str] = None


@dataclass(frozen=True)
class ModelCandidate:
    id: str
    model_id: str
    source: str
    role: str = "chat"
    backend_type: str = "llama_cpp"
    slot_id: Optional[str] = None
    upstream_name: Optional[str] = None
    base_url: str = ""
    context_size: int = 0
    supports_tools: bool = False
    supports_vision: bool = False
    supports_json_mode: bool = False
    health: str = "unknown"
    latency_hint_ms: Optional[float] = None
    quality_hint: float = 0.5
    resource_cost_hint: float = 0.5
    reliability_hint: Optional[float] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "model_id": self.model_id,
            "source": self.source,
            "role": self.role,
            "backend_type": self.backend_type,
            "slot_id": self.slot_id,
            "upstream_name": self.upstream_name,
            "context_size": self.context_size,
            "health": self.health,
            "capabilities": {
                "tools": self.supports_tools,
                "vision": self.supports_vision,
                "json_mode": self.supports_json_mode,
            },
            "hints": {
                "latency_ms": self.latency_hint_ms,
                "quality": self.quality_hint,
                "resource_cost": self.resource_cost_hint,
                "reliability": self.reliability_hint,
            },
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class RankedCandidate:
    candidate: ModelCandidate
    score: float
    reason_codes: list[str]
    score_inputs: dict[str, Any]

    def public_dict(self) -> dict[str, Any]:
        return {
            **self.candidate.public_dict(),
            "score": round(self.score, 4),
            "reason_codes": list(self.reason_codes),
            "score_inputs": dict(self.score_inputs),
        }


def _bool_from_sources(*values: Any, default: bool = False) -> bool:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    return default


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _pick(body: dict[str, Any], routing: dict[str, Any], metadata: dict[str, Any], key: str, default: Any = None) -> Any:
    if key in routing:
        return routing[key]
    if key in body:
        return body[key]
    if key in metadata:
        return metadata[key]
    return default


def _positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _normalize_strategy(value: Any) -> str:
    raw = str(value or "balanced_local").strip().lower().replace("-", "_")
    if raw == "balanced":
        return "balanced_local"
    return raw if raw in VALID_STRATEGIES else "balanced_local"


def _has_vision_content(messages: Any) -> bool:
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in {"image_url", "image", "input_image"}:
                    return True
    return False


def _has_tools(body: dict[str, Any]) -> bool:
    if body.get("tools"):
        choice = body.get("tool_choice")
        return not (isinstance(choice, str) and choice == "none")
    choice = body.get("tool_choice")
    return isinstance(choice, dict) or (isinstance(choice, str) and choice not in {"", "none"})


def _has_json_mode(body: dict[str, Any]) -> bool:
    response_format = body.get("response_format")
    return isinstance(response_format, dict) and response_format.get("type") in {"json_object", "json_schema"}


def role_from_task_type(task_type: str) -> str:
    return _TASK_TO_ROLE.get(str(task_type or "").lower(), "chat")


def role_from_chat_body(body: dict[str, Any]) -> str:
    routing = _dict_or_empty(body.get("routing"))
    metadata = _dict_or_empty(body.get("metadata"))
    explicit = _pick(body, routing, metadata, "role")
    if explicit:
        return str(explicit)
    task_type = str(_pick(body, routing, metadata, "task_type", "chat") or "chat").lower()
    resolution = resolve_alias(body.get("model"), task_type=task_type)
    return resolution.role if resolution.recognized and resolution.role else role_from_task_type(task_type)


def required_capabilities_from_chat_body(body: dict[str, Any], *, role: str | None = None) -> RoutingNeeds:
    routing = _dict_or_empty(body.get("routing"))
    metadata = _dict_or_empty(body.get("metadata"))
    privacy_mode = str(_pick(body, routing, metadata, "privacy_mode", "unknown") or "unknown").lower()
    estimated = _positive_int(_pick(body, routing, metadata, "estimated_tokens"))
    return RoutingNeeds(
        role=str(_pick(body, routing, metadata, "role", role or role_from_chat_body(body)) or "chat"),
        task_type=str(_pick(body, routing, metadata, "task_type", "chat") or "chat"),
        requires_tools=_bool_from_sources(
            _pick(body, routing, metadata, "requires_tools"),
            _has_tools(body),
        ),
        requires_vision=_bool_from_sources(
            _pick(body, routing, metadata, "requires_vision"),
            _has_vision_content(body.get("messages")),
        ),
        requires_json_mode=_bool_from_sources(
            _pick(body, routing, metadata, "requires_json_mode"),
            _has_json_mode(body),
        ),
        requires_long_context=_bool_from_sources(_pick(body, routing, metadata, "requires_long_context")),
        estimated_tokens=estimated,
        local_only=_bool_from_sources(_pick(body, routing, metadata, "local_only")) or privacy_mode == "local_only",
        strategy=_normalize_strategy(_pick(body, routing, metadata, "routing_strategy", _pick(body, routing, metadata, "strategy"))),
        preferred_slot=str(_pick(body, routing, metadata, "preferred_slot", "") or "").strip() or None,
    )


def _float_hint(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(parsed) or math.isinf(parsed):
        return default
    return max(0.0, min(1.0, parsed))


def _role_quality_default(role: str) -> float:
    return {
        "chat": 0.75,
        "utility": 0.55,
        "scribe": 0.65,
        "embed": 0.35,
        "embedding": 0.35,
    }.get(role, 0.5)


def _role_latency_default(role: str) -> float:
    return {
        "utility": 80.0,
        "embed": 60.0,
        "embedding": 60.0,
        "scribe": 140.0,
        "chat": 180.0,
    }.get(role, 180.0)


def _resource_cost_hint(slot: dict[str, Any], role: str) -> float:
    if "resource_cost_hint" in slot:
        return _float_hint(slot.get("resource_cost_hint"), 0.5)
    try:
        gpu_layers = int(slot.get("gpu_layers", -1))
    except (TypeError, ValueError):
        gpu_layers = -1
    if gpu_layers == 0:
        return 0.25
    if role in {"utility", "embed", "embedding"}:
        return 0.6
    return 1.0


def apply_evaluation_hints(
    slots: Iterable[dict[str, Any]], snapshot: Optional[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge the latest deterministic evaluation into slot hints."""
    payload = snapshot.get("payload", snapshot) if isinstance(snapshot, dict) else {}
    models = payload.get("models") if isinstance(payload, dict) else []
    by_model = {
        str(item.get("model_id")): item
        for item in models or []
        if isinstance(item, dict) and item.get("model_id")
    }
    out: list[dict[str, Any]] = []
    for slot in slots:
        row = dict(slot)
        model_id = str(row.get("router_default_model") or row.get("model_id") or "")
        evaluated = by_model.get(model_id)
        if evaluated:
            roles = evaluated.get("roles") if isinstance(evaluated.get("roles"), dict) else {}
            role = "embed" if row.get("role") == "embedding" else str(row.get("role") or "chat")
            metrics = roles.get(role) or roles.get("chat") or next(iter(roles.values()), {})
            if isinstance(metrics, dict):
                row["quality_hint"] = metrics.get("pass_rate", row.get("quality_hint"))
                row["latency_hint_ms"] = metrics.get(
                    "median_latency_ms", row.get("latency_hint_ms")
                )
                row["resource_cost_hint"] = metrics.get(
                    "resource_cost_hint", row.get("resource_cost_hint")
                )
                row["reliability_hint"] = metrics.get("reliability")
                row["evaluation"] = {
                    "schema_version": payload.get("schema_version"),
                    "generated_at": payload.get("generated_at"),
                    "fingerprint": evaluated.get("fingerprint"),
                    "role": role,
                    "metrics": metrics,
                }
        out.append(row)
    return out


def build_slot_candidates(slots: Iterable[dict[str, Any]]) -> list[ModelCandidate]:
    candidates: list[ModelCandidate] = []
    for slot in slots:
        if not isinstance(slot, dict) or not slot.get("enabled", True):
            continue
        slot_id = str(slot.get("id") or slot.get("slot_id") or "").strip()
        if not slot_id:
            continue
        role = str(slot.get("role") or "chat").strip().lower()
        model_id = str(slot.get("router_default_model") or slot.get("model_id") or slot_id).strip() or slot_id
        context_size = _positive_int(slot.get("context_size") or slot.get("hard_ctx")) or 0
        supports_tools = _bool_from_sources(slot.get("supports_tools"), default=role in {"chat", "utility", "scribe"})
        supports_vision = _bool_from_sources(slot.get("supports_vision"), bool(slot.get("mmproj_path")))
        supports_json = _bool_from_sources(slot.get("supports_json_mode"), default=role not in {"embed", "embedding"})
        candidates.append(
            ModelCandidate(
                id=slot_id,
                model_id=model_id,
                source="local_fleet",
                role="embed" if role == "embedding" else role,
                backend_type=str(slot.get("backend_type") or "llama_cpp"),
                slot_id=slot_id,
                base_url=str(slot.get("base_url") or ""),
                context_size=context_size,
                supports_tools=supports_tools,
                supports_vision=supports_vision,
                supports_json_mode=supports_json,
                health=str(slot.get("health") or "unknown"),
                latency_hint_ms=float(slot.get("latency_hint_ms") or _role_latency_default(role)),
                quality_hint=_float_hint(slot.get("quality_hint"), _role_quality_default(role)),
                resource_cost_hint=_resource_cost_hint(slot, role),
                reliability_hint=(
                    _float_hint(slot.get("reliability_hint"), 0.0)
                    if slot.get("reliability_hint") is not None
                    else None
                ),
                metadata={
                    "router_mode": bool(slot.get("router_mode", False)),
                    "parallel_slots": slot.get("parallel_slots"),
                    **({"evaluation": slot["evaluation"]} if slot.get("evaluation") else {}),
                },
            )
        )
    return candidates


def build_upstream_candidates(upstream_rows: Iterable[dict[str, Any]]) -> list[ModelCandidate]:
    """Candidates for upstream models declared eligible for auto-routing.

    *upstream_rows* are ``UpstreamConfig.describe()`` dicts (plain data — this
    module stays import-free of the registry). Only serving upstreams with an
    explicit ``models`` list yield candidates; capabilities come from the
    declared list, never guessed. ``local_score`` stays 0 for these, so a
    healthy local slot always outranks them under every strategy.
    """
    candidates: list[ModelCandidate] = []
    for row in upstream_rows:
        if not isinstance(row, dict) or not row.get("serves_inference"):
            continue
        name = str(row.get("name") or "").strip()
        base_url = str(row.get("base_url") or "").strip()
        models = row.get("models")
        if not name or not base_url or not isinstance(models, (list, tuple)):
            continue
        capabilities = row.get("capabilities")
        capabilities = set(capabilities) if isinstance(capabilities, (list, tuple)) else set()
        per_model = row.get("model_capabilities")
        per_model = per_model if isinstance(per_model, dict) else {}
        for model in models:
            model_id = str(model).strip()
            if not model_id:
                continue
            model_caps = per_model.get(model_id)
            if isinstance(model_caps, (list, tuple)):
                caps = set(model_caps)
            else:
                caps = capabilities
            candidates.append(
                ModelCandidate(
                    id=f"{name}/{model_id}",
                    model_id=model_id,
                    source="upstream",
                    role="chat",
                    backend_type=str(row.get("type") or "openai_compatible"),
                    slot_id=None,
                    upstream_name=name,
                    base_url=base_url,
                    context_size=0,
                    supports_tools="tools" in caps,
                    supports_vision="vision" in caps,
                    supports_json_mode="json_mode" in caps,
                    health="unknown",
                    latency_hint_ms=None,
                    quality_hint=_role_quality_default("chat"),
                    # Upstreams spend no local VRAM; low local resource cost.
                    resource_cost_hint=0.25,
                    metadata={"upstream": name},
                )
            )
    return candidates


def _target_context(needs: RoutingNeeds) -> int:
    targets = [0]
    if needs.estimated_tokens:
        targets.append(needs.estimated_tokens)
    if needs.requires_long_context:
        targets.append(32768)
    return max(targets)


def _latency_score(latency_ms: Optional[float]) -> float:
    if latency_ms is None:
        return 0.5
    return max(0.0, min(1.0, 1.0 - (float(latency_ms) / 1000.0)))


def _health_score(health: str) -> float:
    if health == "healthy":
        return 1.0
    if health == "unknown":
        return 0.55
    return 0.0


def _role_score(candidate: ModelCandidate, needs: RoutingNeeds) -> float:
    if candidate.slot_id and needs.preferred_slot and candidate.slot_id == needs.preferred_slot:
        return 1.25
    if candidate.role == needs.role:
        return 1.0
    if needs.requires_tools and candidate.supports_tools and candidate.role == "utility":
        return 0.9
    if candidate.role == "chat":
        return 0.65
    return 0.45


def _candidate_rejections(candidate: ModelCandidate, needs: RoutingNeeds, target_context: int) -> list[str]:
    rejected: list[str] = []
    if needs.local_only and candidate.source != "local_fleet":
        rejected.append("filtered_cloud_by_local_only")
    if candidate.health in {"unhealthy", "disabled"}:
        rejected.append("filtered_unhealthy")
    if needs.requires_tools and not candidate.supports_tools:
        rejected.append("filtered_missing_tools")
    if needs.requires_vision and not candidate.supports_vision:
        rejected.append("filtered_missing_vision")
    if needs.requires_json_mode and not candidate.supports_json_mode:
        rejected.append("filtered_missing_json_mode")
    if target_context and candidate.context_size and candidate.context_size < target_context:
        rejected.append("filtered_context_too_small")
    return rejected


def rank_candidates(
    needs: RoutingNeeds,
    candidates: Iterable[ModelCandidate],
    *,
    strategy: str | None = None,
) -> list[RankedCandidate]:
    selected_strategy = _normalize_strategy(strategy or needs.strategy)
    target_context = _target_context(needs)
    ranked: list[RankedCandidate] = []

    for candidate in candidates:
        rejected = _candidate_rejections(candidate, needs, target_context)
        if rejected:
            continue

        context_score = 0.5
        if target_context and candidate.context_size:
            context_score = max(0.0, min(1.0, candidate.context_size / target_context))
        elif candidate.context_size:
            context_score = max(0.0, min(1.0, candidate.context_size / 65536.0))

        inputs = {
            "strategy": selected_strategy,
            "role_score": _role_score(candidate, needs),
            "health_score": _health_score(candidate.health),
            "context_score": context_score,
            "context_size": candidate.context_size,
            "quality_hint": candidate.quality_hint,
            "latency_score": _latency_score(candidate.latency_hint_ms),
            "resource_cost_hint": candidate.resource_cost_hint,
            "reliability": candidate.reliability_hint,
            "local_score": 1.0 if candidate.source == "local_fleet" else 0.0,
        }

        if selected_strategy == "fastest":
            score = (
                0.40 * inputs["latency_score"]
                + 0.25 * inputs["role_score"]
                + 0.15 * inputs["health_score"]
                + 0.10 * inputs["context_score"]
                + 0.10 * inputs["local_score"]
            )
        elif selected_strategy == "quality":
            score = (
                0.40 * candidate.quality_hint
                + 0.20 * inputs["context_score"]
                + 0.20 * inputs["role_score"]
                + 0.10 * inputs["health_score"]
                + 0.10 * inputs["local_score"]
            )
        elif selected_strategy == "economy":
            economy_score = 1.0 - candidate.resource_cost_hint
            score = (
                0.35 * economy_score
                + 0.25 * inputs["local_score"]
                + 0.15 * inputs["role_score"]
                + 0.15 * inputs["latency_score"]
                + 0.10 * inputs["health_score"]
            )
        else:
            score = (
                0.25 * inputs["local_score"]
                + 0.35 * inputs["role_score"]
                + 0.20 * inputs["health_score"]
                + 0.05 * inputs["context_score"]
                + 0.05 * candidate.quality_hint
                + 0.10 * inputs["latency_score"]
            )

        reason_codes = ["candidate_ranked"]
        if candidate.metadata.get("evaluation"):
            reason_codes.append("evaluated_model_score")
        if candidate.source == "local_fleet":
            reason_codes.append("local_fleet_preferred")
        if needs.local_only:
            reason_codes.append("local_only_enforced")
        if candidate.slot_id and needs.preferred_slot and candidate.slot_id == needs.preferred_slot:
            reason_codes.append("preferred_slot_matched")
        if candidate.role == needs.role:
            reason_codes.append("role_matched")
        if needs.requires_tools:
            reason_codes.append("capability_tools_match")
        if needs.requires_vision:
            reason_codes.append("capability_vision_match")
        if needs.requires_json_mode:
            reason_codes.append("capability_json_mode_match")
        if target_context:
            reason_codes.append("context_requirement_met")

        ranked.append(
            RankedCandidate(
                candidate=candidate,
                score=score,
                reason_codes=reason_codes,
                score_inputs=inputs,
            )
        )

    return sorted(
        ranked,
        key=lambda item: (
            -item.score,
            item.candidate.latency_hint_ms if item.candidate.latency_hint_ms is not None else 999999,
            item.candidate.id,
        ),
    )

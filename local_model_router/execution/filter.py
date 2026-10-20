"""Deterministic lane filter. Runs before any decision provider."""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    CallerRole,
    ExecutionCatalog,
    InventorySnapshot,
    LaneRejection,
    LaneSpec,
    ModelClassSpec,
    PlanRequest,
    RouteLabel,
    TaskType,
)

_TASK_CAPABILITY = {
    TaskType.chat: "chat",
    TaskType.coding: "coding",
    TaskType.review: "review",
    TaskType.research: "research",
    TaskType.orchestration: "orchestration",
    TaskType.classification: "classification",
}


@dataclass(frozen=True)
class CandidateLane:
    lane: LaneSpec
    model_class: ModelClassSpec
    selected_model: str


@dataclass
class FilterResult:
    eligible: list[CandidateLane] = field(default_factory=list)
    rejected: list[LaneRejection] = field(default_factory=list)


def filter_lanes(
    request: PlanRequest,
    catalog: ExecutionCatalog,
    inventory: InventorySnapshot,
) -> FilterResult:
    loaded = set(inventory.loaded_models)
    needed = _TASK_CAPABILITY[request.task_type]
    result = FilterResult()
    for lane in catalog.lanes:
        code = _reject_reason(request, lane, catalog, inventory, loaded, needed)
        if code is not None:
            result.rejected.append(LaneRejection(label=lane.label.value, code=code))
            continue
        model_class = catalog.model_classes[lane.model_class]
        selected = next(name for name in model_class.prefer if name in loaded)
        result.eligible.append(
            CandidateLane(lane=lane, model_class=model_class, selected_model=selected)
        )
    return result


def _reject_reason(
    request: PlanRequest,
    lane: LaneSpec,
    catalog: ExecutionCatalog,
    inventory: InventorySnapshot,
    loaded: set[str],
    needed: str,
) -> str | None:
    if lane.label == RouteLabel.swarm and not request.constraints.allow_swarm:
        return "swarm_not_allowed"
    model_class = catalog.model_classes.get(lane.model_class)
    if model_class is None:
        return "unknown_model_class"
    if request.caller == CallerRole.worker and model_class.reserved_for_main:
        return "reserved_for_main"
    if needed not in lane.capabilities:
        return "capability_mismatch"
    if not request.constraints.allow_remote and model_class.remote:
        return "remote_not_allowed"
    if inventory.free_vram_mb < model_class.max_vram_mb:
        return "insufficient_vram"
    cap = request.constraints.max_vram_mb
    if cap is not None and model_class.max_vram_mb > cap:
        return "vram_cap"
    if model_class.min_context < request.constraints.min_context:
        return "context_too_small"
    if not any(name in loaded for name in model_class.prefer):
        return "model_unavailable"
    return None

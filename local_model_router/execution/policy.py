"""Local route choice and the threshold gate. No Jev client lives here."""
from __future__ import annotations

import logging
from typing import Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .filter import CandidateLane, FilterResult, filter_lanes
from .models import (
    ExecutionCatalog,
    ExecutionPlan,
    ExecutionSettings,
    InventorySnapshot,
    PlanRequest,
    RouteLabel,
    TaskType,
)

logger = logging.getLogger(__name__)

_HIGH = 0.55
_ORDER: dict[TaskType, dict[str, list[RouteLabel]]] = {
    TaskType.coding: {
        "low": [RouteLabel.fast_local, RouteLabel.balanced_local, RouteLabel.review_gate, RouteLabel.human_review],
        "mid": [RouteLabel.balanced_local, RouteLabel.moe_reasoning, RouteLabel.fast_local, RouteLabel.review_gate, RouteLabel.swarm, RouteLabel.human_review],
        "high": [RouteLabel.moe_reasoning, RouteLabel.balanced_local, RouteLabel.swarm, RouteLabel.planner_frontier, RouteLabel.review_gate, RouteLabel.fast_local, RouteLabel.human_review],
    },
    TaskType.review: {
        "low": [RouteLabel.review_gate, RouteLabel.balanced_local, RouteLabel.human_review],
        "mid": [RouteLabel.review_gate, RouteLabel.balanced_local, RouteLabel.human_review],
        "high": [RouteLabel.review_gate, RouteLabel.balanced_local, RouteLabel.moe_reasoning, RouteLabel.human_review],
    },
    TaskType.orchestration: {
        "low": [RouteLabel.balanced_local, RouteLabel.planner_frontier, RouteLabel.human_review],
        "mid": [RouteLabel.swarm, RouteLabel.planner_frontier, RouteLabel.moe_reasoning, RouteLabel.balanced_local, RouteLabel.human_review],
        "high": [RouteLabel.swarm, RouteLabel.planner_frontier, RouteLabel.moe_reasoning, RouteLabel.balanced_local, RouteLabel.human_review],
    },
    TaskType.research: {
        "low": [RouteLabel.balanced_local, RouteLabel.fast_local, RouteLabel.human_review],
        "mid": [RouteLabel.moe_reasoning, RouteLabel.balanced_local, RouteLabel.human_review],
        "high": [RouteLabel.moe_reasoning, RouteLabel.planner_frontier, RouteLabel.balanced_local, RouteLabel.human_review],
    },
    TaskType.classification: {
        "low": [RouteLabel.fast_local, RouteLabel.deterministic_tool, RouteLabel.human_review],
        "mid": [RouteLabel.fast_local, RouteLabel.balanced_local, RouteLabel.human_review],
        "high": [RouteLabel.balanced_local, RouteLabel.fast_local, RouteLabel.human_review],
    },
    TaskType.chat: {
        "low": [RouteLabel.fast_local, RouteLabel.balanced_local, RouteLabel.human_review, RouteLabel.review_gate],
        "mid": [RouteLabel.balanced_local, RouteLabel.fast_local, RouteLabel.human_review],
        "high": [RouteLabel.moe_reasoning, RouteLabel.balanced_local, RouteLabel.planner_frontier, RouteLabel.human_review],
    },
}
_CONFIDENCE = {"low": 0.9, "mid": 0.8, "high": 0.86}
_MULTI_STEP = ("multi-step", "swarm", "parallel", "refactor", "architect")


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: RouteLabel
    confidence: float = Field(ge=0.0, le=1.0)
    distribution: list[RouteLabel] = Field(default_factory=list)


class DecisionProvider(Protocol):
    def choose(self, request: PlanRequest, eligible: list[CandidateLane]) -> Decision: ...


class HeuristicDecision:
    """Stand-in for a future Jev provider. Same choose() contract, no network."""

    def choose(self, request: PlanRequest, eligible: list[CandidateLane]) -> Decision:
        bucket = _bucket(score_complexity(request))
        present = {item.lane.label for item in eligible}
        ordered = [label for label in _ORDER[request.task_type][bucket] if label in present]
        ordered.extend(item.lane.label for item in eligible if item.lane.label not in ordered)
        return Decision(label=ordered[0], confidence=_CONFIDENCE[bucket], distribution=ordered)


class PolicyGate:
    def __init__(self, settings: ExecutionSettings) -> None:
        self._settings = settings

    def apply(self, request: PlanRequest, filtered: FilterResult, decision: Decision) -> ExecutionPlan:
        by_label = {item.lane.label: item for item in filtered.eligible}
        if decision.label not in by_label:
            return _fail_open(request, self._settings, filtered, by_label, "decision_rejected")
        label = decision.label
        escalated = False
        if (
            request.task_type == TaskType.coding
            and score_complexity(request) >= _HIGH
            and label == RouteLabel.fast_local
        ):
            for nxt in (RouteLabel.balanced_local, RouteLabel.moe_reasoning):
                if nxt in by_label:
                    label = nxt
                    escalated = True
                    break
        if decision.confidence < by_label[label].lane.confidence_min:
            escalated = True
            nxt = _meeting_confidence(decision.distribution, by_label, decision.confidence, skip=label)
            if nxt is None and RouteLabel.review_gate in by_label:
                nxt = RouteLabel.review_gate
            if nxt is None:
                return _human_review(request, self._settings, filtered, "confidence_below_minimum")
            label = nxt
        return _from_lane(
            request,
            self._settings,
            filtered,
            by_label[label],
            decision.confidence,
            escalated,
            "heuristic",
        )


def build_plan(
    request: PlanRequest,
    catalog: ExecutionCatalog,
    inventory: InventorySnapshot,
    provider: Optional[DecisionProvider] = None,
) -> ExecutionPlan:
    filtered = filter_lanes(request, catalog, inventory)
    if not filtered.eligible:
        return _human_review(request, catalog.settings, filtered, "no_eligible_lane")
    decider = provider or HeuristicDecision()
    try:
        decision = decider.choose(request, filtered.eligible)
    except Exception as exc:
        logger.warning("execution decision failed (%s)", type(exc).__name__)
        by_label = {item.lane.label: item for item in filtered.eligible}
        return _fail_open(request, catalog.settings, filtered, by_label, "decision_failed")
    return PolicyGate(catalog.settings).apply(request, filtered, decision)


def score_complexity(request: PlanRequest) -> float:
    text = _text(request).lower()
    files = len(request.repo.files) if request.repo is not None else 0
    score = 0.05
    if len(text) > 6000:
        score += 0.4
    elif len(text) > 2000:
        score += 0.2
    if files >= 4:
        score += 0.3
    elif files >= 1:
        score += 0.1
    if any(word in text for word in _MULTI_STEP):
        score += 0.3
    if request.constraints.require_json:
        score += 0.1
    if request.task_type in {TaskType.orchestration, TaskType.research}:
        score += 0.25
    elif request.task_type == TaskType.coding:
        score += 0.15
    return min(score, 1.0)


def _bucket(score: float) -> str:
    if score >= _HIGH:
        return "high"
    if score >= 0.3:
        return "mid"
    return "low"


def _text(request: PlanRequest) -> str:
    parts: list[str] = []
    if request.user_goal:
        parts.append(request.user_goal)
    for message in request.messages:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)


def _meeting_confidence(
    distribution: list[RouteLabel],
    by_label: dict[RouteLabel, CandidateLane],
    confidence: float,
    *,
    skip: RouteLabel,
) -> Optional[RouteLabel]:
    passed = skip not in distribution
    for label in distribution:
        if not passed:
            if label == skip:
                passed = True
            continue
        lane = by_label.get(label)
        if lane is not None and confidence >= lane.lane.confidence_min:
            return label
    return None


def _fail_open(
    request: PlanRequest,
    settings: ExecutionSettings,
    filtered: FilterResult,
    by_label: dict[RouteLabel, CandidateLane],
    reason: str,
) -> ExecutionPlan:
    lane = by_label.get(settings.fail_open_lane)
    if lane is None:
        return _human_review(request, settings, filtered, "fail_open_unavailable")
    plan = _from_lane(request, settings, filtered, lane, 0.0, True, "fail_open")
    plan.reasons.insert(0, reason)
    return plan


def _human_review(
    request: PlanRequest,
    settings: ExecutionSettings,
    filtered: FilterResult,
    reason: str,
) -> ExecutionPlan:
    return ExecutionPlan(
        request_id=request.request_id,
        route_label=RouteLabel.human_review,
        agent_profile="judge",
        runtime="none",
        confidence=0.0,
        escalated=True,
        reasons=[reason],
        rejected=list(filtered.rejected),
        eligible=[item.lane.label.value for item in filtered.eligible],
        policy_revision=settings.policy_revision,
        decision_provider="none",
    )


def _from_lane(
    request: PlanRequest,
    settings: ExecutionSettings,
    filtered: FilterResult,
    chosen: CandidateLane,
    confidence: float,
    escalated: bool,
    provider: str,
) -> ExecutionPlan:
    lane = chosen.lane
    workers = None
    if lane.label == RouteLabel.swarm:
        workers = lane.max_workers or settings.max_workers
    reasons = ["selected"]
    if escalated:
        reasons.append("escalated")
    return ExecutionPlan(
        request_id=request.request_id,
        route_label=lane.label,
        model=chosen.selected_model,
        model_class=chosen.model_class.name,
        agent_profile=lane.agent_profile,
        runtime=lane.runtime,
        confidence=confidence,
        escalated=escalated,
        prepare_steps=list(lane.prepare_steps),
        max_workers=workers,
        reasons=reasons,
        rejected=list(filtered.rejected),
        eligible=[item.lane.label.value for item in filtered.eligible],
        policy_revision=settings.policy_revision,
        reserved_for_main=chosen.model_class.reserved_for_main,
        decision_provider=provider,
    )

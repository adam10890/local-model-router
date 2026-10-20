"""DTOs for the dry-run execution planner.

The planner returns a route. It does not call a model, and it does not copy
prompt text into the plan.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)


class RouteLabel(str, Enum):
    deterministic_tool = "deterministic_tool"
    fast_local = "fast_local"
    balanced_local = "balanced_local"
    moe_reasoning = "moe_reasoning"
    planner_frontier = "planner_frontier"
    swarm = "swarm"
    review_gate = "review_gate"
    human_review = "human_review"


class CallerRole(str, Enum):
    main = "main"
    worker = "worker"


class TaskType(str, Enum):
    chat = "chat"
    coding = "coding"
    review = "review"
    research = "research"
    orchestration = "orchestration"
    classification = "classification"


class Constraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latency_ms: int = 15000
    max_vram_mb: Optional[int] = None
    max_cost_units: Optional[float] = None
    allow_swarm: bool = True
    allow_remote: bool = False
    allow_tools: bool = True
    min_context: int = 8192
    require_json: bool = False


class RepoHint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: Optional[str] = None
    files: list[str] = Field(default_factory=list)
    diff: Optional[str] = None


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    parent_session_id: Optional[str] = None
    harness: str = "hermes"
    task_type: TaskType = TaskType.chat
    messages: list[dict[str, Any]]
    user_goal: Optional[str] = None
    repo: Optional[RepoHint] = None
    constraints: Constraints = Field(default_factory=Constraints)
    metadata: dict[str, Any] = Field(default_factory=dict)
    caller: CallerRole = CallerRole.main


class ModelClassSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    prefer: list[str]
    max_vram_mb: int = Field(ge=0)
    min_context: int = 8192
    architecture: Literal["dense", "moe", "unknown"] = "unknown"
    remote: bool = False
    reserved_for_main: bool = False


class LaneSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: RouteLabel
    description: str
    model_class: str
    agent_profile: str
    runtime: str
    prepare_steps: list[str] = Field(default_factory=list)
    confidence_min: float = Field(default=0.0, ge=0.0, le=1.0)
    capabilities: list[str] = Field(default_factory=list)
    max_workers: Optional[int] = Field(default=None, ge=1)


class ExecutionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    policy_revision: str = "2026-10-19.1"
    fail_open_lane: RouteLabel = RouteLabel.balanced_local
    max_workers: int = Field(default=3, ge=1)


class InventorySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    free_vram_mb: float = Field(default=0.0, ge=0.0)
    loaded_models: list[str] = Field(default_factory=list)


class LaneRejection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    code: str


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    route_label: RouteLabel
    model: Optional[str] = None
    model_class: str = ""
    agent_profile: str = ""
    runtime: str = "none"
    confidence: float = 0.0
    escalated: bool = False
    prepare_steps: list[str] = Field(default_factory=list)
    max_workers: Optional[int] = None
    reasons: list[str] = Field(default_factory=list)
    rejected: list[LaneRejection] = Field(default_factory=list)
    eligible: list[str] = Field(default_factory=list)
    policy_revision: str = ""
    reserved_for_main: bool = False
    decision_provider: str = "heuristic"


@dataclass
class ExecutionCatalog:
    settings: ExecutionSettings = field(default_factory=ExecutionSettings)
    lanes: list[LaneSpec] = field(default_factory=list)
    model_classes: dict[str, ModelClassSpec] = field(default_factory=dict)


def load_execution_catalog(conf_dir: str | Path) -> ExecutionCatalog:
    """Load planner config. A missing or broken file stays disabled."""
    root = Path(conf_dir)
    return ExecutionCatalog(
        settings=_load_settings(root / "execution.yaml"),
        model_classes=_load_classes(root / "execution" / "model_classes.yaml"),
        lanes=_load_lanes(root / "execution" / "lanes.yaml"),
    )


def _load_settings(path: Path) -> ExecutionSettings:
    block = _mapping(path)
    if block is None:
        return ExecutionSettings()
    nested = block.get("execution")
    source = nested if isinstance(nested, dict) else block
    try:
        return ExecutionSettings.model_validate(
            {
                "enabled": source.get("enabled", False),
                "policy_revision": str(source.get("policy_revision", "2026-10-19.1")),
                "fail_open_lane": source.get("fail_open_lane", RouteLabel.balanced_local),
                "max_workers": source.get("max_workers", 3),
            }
        )
    except ValidationError as exc:
        logger.warning("Execution settings ignored (%s): %s", path, exc.error_count())
        return ExecutionSettings()


def _load_classes(path: Path) -> dict[str, ModelClassSpec]:
    block = _mapping(path)
    raw = (block or {}).get("model_classes")
    if not isinstance(raw, dict):
        return {}
    classes: dict[str, ModelClassSpec] = {}
    for name, item in raw.items():
        if not isinstance(item, dict):
            logger.warning("Execution model class ignored (%s): %s", path, name)
            continue
        try:
            classes[str(name)] = ModelClassSpec.model_validate({**item, "name": str(name)})
        except ValidationError:
            logger.warning("Execution model class ignored (%s): %s", path, name)
    return classes


def _load_lanes(path: Path) -> list[LaneSpec]:
    block = _mapping(path)
    raw = (block or {}).get("lanes")
    if not isinstance(raw, list):
        return []
    lanes: list[LaneSpec] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            lanes.append(LaneSpec.model_validate(item))
        except ValidationError:
            logger.warning("Execution lane ignored (%s)", path)
    return lanes


def _mapping(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Execution config ignored (could not read %s): %s", path, type(exc).__name__)
        return None
    if not isinstance(raw, dict):
        logger.warning("Execution config ignored (%s: top level must be a mapping)", path)
        return None
    return raw

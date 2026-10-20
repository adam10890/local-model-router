"""Dry-run execution planner: filter, heuristic, policy gate. No fleet, no Jev."""
from __future__ import annotations

import textwrap
from pathlib import Path

from starlette.testclient import TestClient

from local_model_router.execution.filter import CandidateLane, FilterResult, filter_lanes
from local_model_router.execution.models import (
    CallerRole,
    Constraints,
    ExecutionCatalog,
    ExecutionSettings,
    InventorySnapshot,
    LaneSpec,
    ModelClassSpec,
    PlanRequest,
    RouteLabel,
    TaskType,
    load_execution_catalog,
)
from local_model_router.execution.policy import Decision, PolicyGate, build_plan
from local_model_router.service.app import create_app

_CONF = Path(__file__).resolve().parents[1] / "conf"


def _request(**overrides) -> PlanRequest:
    payload = {
        "request_id": "r1",
        "messages": [{"role": "user", "content": "hi"}],
    }
    payload.update(overrides)
    return PlanRequest.model_validate(payload)


def _class(name: str, model: str, *, vram: int = 1000, reserved: bool = False) -> ModelClassSpec:
    return ModelClassSpec(name=name, prefer=[model], max_vram_mb=vram, reserved_for_main=reserved)


def _lane(label: RouteLabel, model_class: str, caps: list[str], *, confidence_min: float = 0.0) -> LaneSpec:
    return LaneSpec(
        label=label,
        description=label.value,
        model_class=model_class,
        agent_profile="coder",
        runtime="thin",
        capabilities=caps,
        confidence_min=confidence_min,
    )


def test_packaged_catalog_stays_disabled():
    catalog = load_execution_catalog(_CONF)
    assert catalog.settings.enabled is False
    assert "local_small" in catalog.model_classes
    assert any(lane.label == RouteLabel.swarm for lane in catalog.lanes)


def test_lane_without_vram_is_rejected_before_decision():
    catalog = ExecutionCatalog(
        lanes=[_lane(RouteLabel.moe_reasoning, "big", ["coding"])],
        model_classes={"big": _class("big", "heavy", vram=8000)},
    )
    called: list[bool] = []

    class Boom:
        def choose(self, request, eligible):
            called.append(True)
            raise AssertionError("decision ran")

    plan = build_plan(
        _request(task_type=TaskType.coding),
        catalog,
        InventorySnapshot(free_vram_mb=100, loaded_models=["heavy"]),
        Boom(),
    )
    assert called == []
    assert plan.route_label == RouteLabel.human_review
    assert any(item.code == "insufficient_vram" for item in plan.rejected)


def test_allow_swarm_false_removes_swarm_before_decision():
    catalog = ExecutionCatalog(
        lanes=[
            _lane(RouteLabel.swarm, "mid", ["orchestration"]),
            _lane(RouteLabel.balanced_local, "mid", ["orchestration"]),
        ],
        model_classes={"mid": _class("mid", "coder")},
    )
    request = _request(
        task_type=TaskType.orchestration,
        constraints=Constraints(allow_swarm=False),
    )
    inventory = InventorySnapshot(free_vram_mb=8000, loaded_models=["coder"])
    filtered = filter_lanes(request, catalog, inventory)
    assert RouteLabel.swarm not in {item.lane.label for item in filtered.eligible}
    assert any(item.code == "swarm_not_allowed" for item in filtered.rejected)
    assert build_plan(request, catalog, inventory).route_label != RouteLabel.swarm


def test_low_confidence_escalates_to_the_next_lane():
    small = _class("small", "m")
    fast = _lane(RouteLabel.fast_local, "small", ["chat"], confidence_min=0.72)
    balanced = _lane(RouteLabel.balanced_local, "small", ["chat"], confidence_min=0.40)
    eligible = [
        CandidateLane(fast, small, "m"),
        CandidateLane(balanced, small, "m"),
    ]
    decision = Decision(
        label=RouteLabel.fast_local,
        confidence=0.5,
        distribution=[RouteLabel.fast_local, RouteLabel.balanced_local],
    )
    plan = PolicyGate(ExecutionSettings()).apply(
        _request(),
        FilterResult(eligible=eligible),
        decision,
    )
    assert plan.route_label == RouteLabel.balanced_local
    assert plan.escalated is True


def test_failed_provider_falls_open():
    catalog = ExecutionCatalog(
        settings=ExecutionSettings(enabled=True, fail_open_lane=RouteLabel.balanced_local),
        lanes=[_lane(RouteLabel.balanced_local, "mid", ["chat"], confidence_min=0.68)],
        model_classes={"mid": _class("mid", "coder")},
    )

    class Down:
        def choose(self, request, eligible):
            raise TimeoutError("down")

    plan = build_plan(
        _request(),
        catalog,
        InventorySnapshot(free_vram_mb=8000, loaded_models=["coder"]),
        Down(),
    )
    assert plan.route_label == RouteLabel.balanced_local
    assert plan.confidence == 0
    assert plan.escalated is True
    assert plan.decision_provider == "fail_open"


def test_worker_does_not_receive_a_main_reserved_lane():
    catalog = ExecutionCatalog(
        lanes=[
            _lane(RouteLabel.planner_frontier, "heavy", ["orchestration"]),
            _lane(RouteLabel.balanced_local, "mid", ["orchestration"]),
        ],
        model_classes={
            "heavy": _class("heavy", "frontier", reserved=True),
            "mid": _class("mid", "coder"),
        },
    )
    request = _request(task_type=TaskType.orchestration, caller=CallerRole.worker)
    inventory = InventorySnapshot(free_vram_mb=8000, loaded_models=["frontier", "coder"])
    filtered = filter_lanes(request, catalog, inventory)
    assert any(item.label == "planner_frontier" and item.code == "reserved_for_main" for item in filtered.rejected)
    plan = build_plan(request, catalog, inventory)
    assert plan.route_label == RouteLabel.balanced_local
    assert plan.reserved_for_main is False


_FLEET = textwrap.dedent("""
    active_slots:
      - id: slot_router
        host: localhost
        port: 8080
        role: chat
        enabled: true
        model_id: chat-model
    global:
      backend: remote
""")


class _Hardware:
    def vram_summary(self):
        return {"available_gb": 20.0, "total_gb": 24.0}

    def to_dict(self):
        return {"available": True}


def _app(tmp_path, monkeypatch, *, enabled: bool):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    monkeypatch.setattr("local_model_router.service.app.scan_hardware", lambda: _Hardware())
    (tmp_path / "llama_cpp_servers.yaml").write_text(_FLEET, encoding="utf-8")
    (tmp_path / "execution.yaml").write_text(
        f"execution:\n  enabled: {'true' if enabled else 'false'}\n  fail_open_lane: balanced_local\n",
        encoding="utf-8",
    )
    nest = tmp_path / "execution"
    nest.mkdir()
    (nest / "model_classes.yaml").write_text(
        "model_classes:\n  local_small:\n    prefer: [chat-model]\n    max_vram_mb: 8000\n",
        encoding="utf-8",
    )
    (nest / "lanes.yaml").write_text(
        textwrap.dedent("""
            lanes:
              - label: fast_local
                description: short
                model_class: local_small
                agent_profile: thin_coder
                runtime: thin
                confidence_min: 0.5
                capabilities: [chat]
        """),
        encoding="utf-8",
    )
    return create_app(
        str(tmp_path / "llama_cpp_servers.yaml"),
        apps_path=str(tmp_path / "missing-apps.yaml"),
    )


def test_execution_plan_disabled_is_403(tmp_path, monkeypatch):
    client = TestClient(_app(tmp_path, monkeypatch, enabled=False))
    response = client.post("/v1/execution/plan", json={})
    assert response.status_code == 403
    assert response.json()["error"] == "execution_disabled"


def test_execution_plan_does_not_spawn_or_echo_the_prompt(tmp_path, monkeypatch):
    secret = "hi-secret-prompt"
    client = TestClient(_app(tmp_path, monkeypatch, enabled=True))
    response = client.post(
        "/v1/execution/plan",
        json={"request_id": "t1", "task_type": "chat", "messages": [{"role": "user", "content": secret}]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["route_label"] == "fast_local"
    assert body["max_workers"] is None
    assert secret not in response.text
    capacity = client.get("/v1/swarm/capacity")
    assert capacity.status_code == 200
    assert capacity.json()["running"] == 0

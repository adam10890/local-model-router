"""
Tests for Phase 6 — budget-aware upstream routing.

RoutingIntentHandler gained an optional `budget_status_fn` (a no-arg callable
returning {provider_name: status}). It is consulted ONLY for upstream
candidates, never local slots: "exhausted" providers are dropped from the
candidate pool, "warn" providers are kept but flagged. When the callable is
None (the pre-Phase-6 default), behavior is byte-for-byte unchanged.

Mirrors the style of test_auto_upstream_routing.py — stub health checkers,
no real llama.cpp servers or network calls.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from starlette.testclient import TestClient  # noqa: E402

from local_model_router.service.routing_intent import (  # noqa: E402
    AUTO_UPSTREAMS_ENV,
    RoutingIntentHandler,
    RoutingIntentRequest,
)

_FLEET_CONFIG = """\
active_slots:
  - id: chat
    port: 8080
    host: localhost
    role: chat
    enabled: true
    model_id: mistral-7b-q4
global:
  backend: remote
"""

_UPSTREAM_ROWS = [
    {
        "name": "ollama",
        "type": "openai_compatible",
        "base_url": "http://localhost:11434/v1",
        "serves_inference": True,
        "capabilities": ["chat", "models", "tools"],
        "models": ["llama3.3:70b"],
    },
    {
        "name": "openrouter",
        "type": "openai_compatible",
        "base_url": "http://localhost:9999/v1",
        "serves_inference": True,
        "capabilities": ["chat"],
        "models": ["gpt-oss"],
    },
]


def _make_handler(tmp_path, health="unhealthy", budget_status_fn=None, upstream_rows=None):
    from local_model_router.helpers.llama_cpp_manager import BackendManager

    BackendManager._instance = None
    cfg = tmp_path / "llama_cpp_servers.yaml"
    cfg.write_text(_FLEET_CONFIG)
    mgr = BackendManager(str(cfg))

    class StubChecker:
        async def check_async(self, config):
            return health

        def check(self, config):
            return health

    mgr._health_checker = StubChecker()

    class StubObserver:
        def _make_manager(self):
            return mgr

    rows = _UPSTREAM_ROWS if upstream_rows is None else upstream_rows
    return RoutingIntentHandler(
        StubObserver(),
        upstream_rows_fn=lambda: rows,
        budget_status_fn=budget_status_fn,
    )


def _handle(handler, **req_fields):
    req = RoutingIntentRequest.model_validate(req_fields)
    return asyncio.run(handler.handle(req))


# ---------------------------------------------------------------------------
# Budget-aware filtering of upstream candidates
# ---------------------------------------------------------------------------

class TestBudgetAwareUpstreamRouting:
    def test_exhausted_provider_excluded_and_flagged(self, tmp_path, monkeypatch):
        monkeypatch.setenv(AUTO_UPSTREAMS_ENV, "1")
        handler = _make_handler(
            tmp_path,
            health="unhealthy",  # local fleet down -> falls through to upstream lane
            budget_status_fn=lambda: {"ollama": "exhausted", "openrouter": "ok"},
        )
        decision = _handle(handler, task_type="chat")
        # ollama is excluded outright -> openrouter is the only remaining candidate
        assert decision.selected_upstream == "openrouter"
        assert "upstream_budget_exhausted:ollama" in decision.reason_codes
        assert any("upstream_budget_exhausted:ollama" in w for w in decision.warnings)
        assert decision.budget == {"ollama": "exhausted", "openrouter": "ok"}

    def test_warn_provider_kept_but_flagged(self, tmp_path, monkeypatch):
        monkeypatch.setenv(AUTO_UPSTREAMS_ENV, "1")
        handler = _make_handler(
            tmp_path,
            health="unhealthy",
            budget_status_fn=lambda: {"ollama": "warn"},
            upstream_rows=[_UPSTREAM_ROWS[0]],
        )
        decision = _handle(handler, task_type="chat")
        assert decision.selected_upstream == "ollama"
        assert any("upstream_budget_low:ollama" in w for w in decision.warnings)
        assert decision.budget == {"ollama": "warn"}

    def test_all_exhausted_falls_back_to_no_slot(self, tmp_path, monkeypatch):
        monkeypatch.setenv(AUTO_UPSTREAMS_ENV, "1")
        handler = _make_handler(
            tmp_path,
            health="unhealthy",
            budget_status_fn=lambda: {"ollama": "exhausted", "openrouter": "exhausted"},
        )
        decision = _handle(handler, task_type="chat")
        assert decision.no_slot_available is True
        assert decision.selected_upstream is None
        assert "upstream_budget_exhausted:ollama" in decision.reason_codes
        assert "upstream_budget_exhausted:openrouter" in decision.reason_codes

    def test_budget_status_fn_failure_is_swallowed(self, tmp_path, monkeypatch):
        monkeypatch.setenv(AUTO_UPSTREAMS_ENV, "1")

        def _boom():
            raise RuntimeError("boom")

        handler = _make_handler(tmp_path, health="unhealthy", budget_status_fn=_boom)
        decision = _handle(handler, task_type="chat")
        # A broken source degrades to "no budget data", not a routing outage:
        # upstream fallback still happens as if budget_status_fn were None.
        assert decision.selected_upstream is not None
        assert decision.budget == {}

    def test_healthy_local_still_wins_over_budget_data(self, tmp_path, monkeypatch):
        """Budget status is only ever consulted for the upstream lane; a
        healthy local slot is untouched by it."""
        monkeypatch.setenv(AUTO_UPSTREAMS_ENV, "1")
        handler = _make_handler(
            tmp_path,
            health="healthy",
            budget_status_fn=lambda: {"ollama": "exhausted", "openrouter": "exhausted"},
        )
        decision = _handle(handler, task_type="chat")
        assert decision.selected_source == "local_fleet"
        assert decision.selected_slot_id == "chat"


# ---------------------------------------------------------------------------
# Backward compatibility: no budget_status_fn => identical decisions
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    def test_no_budget_status_fn_identical_decision(self, tmp_path, monkeypatch):
        """A handler built without budget_status_fn (as every pre-Phase-6 test
        does) must produce the same decision as one with an inert (empty-dict)
        budget_status_fn — proving the feature is fully opt-in."""
        monkeypatch.setenv(AUTO_UPSTREAMS_ENV, "1")
        handler_no_budget = _make_handler(tmp_path, health="unhealthy", budget_status_fn=None)
        handler_with_inert_budget = _make_handler(
            tmp_path, health="unhealthy", budget_status_fn=lambda: {}
        )
        d1 = _handle(handler_no_budget, task_type="chat")
        d2 = _handle(handler_with_inert_budget, task_type="chat")
        assert d1.model_dump(exclude={"decision_id"}) == d2.model_dump(exclude={"decision_id"})

    def test_positional_construction_without_budget_status_fn_still_works(self, tmp_path):
        """Existing call sites do RoutingIntentHandler(observer) or
        RoutingIntentHandler(observer, upstream_rows_fn=...) with no third arg."""
        from local_model_router.helpers.llama_cpp_manager import BackendManager

        BackendManager._instance = None
        cfg = tmp_path / "llama_cpp_servers.yaml"
        cfg.write_text(_FLEET_CONFIG)
        mgr = BackendManager(str(cfg))

        class StubChecker:
            async def check_async(self, config):
                return "healthy"

            def check(self, config):
                return "healthy"

        mgr._health_checker = StubChecker()

        class StubObserver:
            def _make_manager(self):
                return mgr

        handler = RoutingIntentHandler(StubObserver())
        decision = _handle(handler, task_type="chat")
        assert decision.no_slot_available is False
        assert decision.budget == {}


# ---------------------------------------------------------------------------
# HTTP surface: new request fields + budget block round-trip
# ---------------------------------------------------------------------------

class TestRoutingRequestEndpoint:
    def test_new_fields_accepted_and_budget_block_present(self, tmp_path):
        from local_model_router.service.app import create_app

        cfg = tmp_path / "llama_cpp_servers.yaml"
        cfg.write_text(_FLEET_CONFIG)
        upstreams_cfg = tmp_path / "upstreams.yaml"
        upstreams_cfg.write_text("upstreams: []\n")

        app = create_app(str(cfg), upstreams_path=str(upstreams_cfg))
        client = TestClient(app)
        resp = client.post(
            "/routing/request",
            json={
                "task_type": "chat",
                "est_input_tokens": 500,
                "est_output_tokens": 200,
                "quality": "best_available",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "budget" in body
        assert isinstance(body["budget"], dict)

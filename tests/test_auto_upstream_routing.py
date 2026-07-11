"""
Tests for opt-in upstream-aware auto-routing (A0_LMM_ROUTER_AUTO_UPSTREAMS).

Local-first contract: declared upstream models only serve as a fallback lane
when no healthy local slot can take the request, and only when the flag is on.
All tests are hermetic — stub health checkers, no network.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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
  - id: utility
    port: 8088
    host: localhost
    role: utility
    enabled: true
global:
  backend: remote
"""

_EMPTY_FLEET = "active_slots: []\nglobal:\n  backend: remote\n"

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
        "name": "disabled_one",
        "type": "openai_compatible",
        "base_url": "http://localhost:9999/v1",
        "serves_inference": False,
        "capabilities": ["chat"],
        "models": ["ghost-model"],
    },
]


def _make_handler(tmp_path, yaml_content=_FLEET_CONFIG, health="healthy", upstream_rows=None):
    from local_model_router.helpers.llama_cpp_manager import BackendManager

    BackendManager._instance = None
    cfg = tmp_path / "llama_cpp_servers.yaml"
    cfg.write_text(yaml_content)
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
    return RoutingIntentHandler(StubObserver(), upstream_rows_fn=lambda: rows)


def _handle(handler, **req_fields):
    req = RoutingIntentRequest.model_validate(req_fields)
    return asyncio.run(handler.handle(req))


# ---------------------------------------------------------------------------
# Flag off (default): behavior identical to before — upstreams never ranked
# ---------------------------------------------------------------------------

class TestFlagOff:
    def test_no_upstream_fallback_when_flag_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv(AUTO_UPSTREAMS_ENV, raising=False)
        handler = _make_handler(tmp_path, health="unhealthy")
        decision = _handle(handler, task_type="chat")
        assert decision.no_slot_available is True
        assert decision.selected_upstream is None

    def test_no_upstream_candidates_in_ranking(self, tmp_path, monkeypatch):
        monkeypatch.delenv(AUTO_UPSTREAMS_ENV, raising=False)
        handler = _make_handler(tmp_path, health="healthy")
        decision = _handle(handler, task_type="chat")
        sources = {c["source"] for c in decision.ranked_candidates}
        assert sources == {"local_fleet"}


# ---------------------------------------------------------------------------
# Flag on: upstream is a fallback lane, never preferred over healthy local
# ---------------------------------------------------------------------------

class TestFlagOn:
    def test_healthy_local_still_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv(AUTO_UPSTREAMS_ENV, "1")
        handler = _make_handler(tmp_path, health="healthy")
        decision = _handle(handler, task_type="chat")
        assert decision.selected_source == "local_fleet"
        assert decision.selected_slot_id == "chat"
        assert decision.selected_upstream is None
        assert "auto_upstreams_considered" in decision.reason_codes

    def test_unhealthy_fleet_falls_back_to_upstream(self, tmp_path, monkeypatch):
        monkeypatch.setenv(AUTO_UPSTREAMS_ENV, "1")
        handler = _make_handler(tmp_path, health="unhealthy")
        decision = _handle(handler, task_type="chat")
        assert decision.no_slot_available is False
        assert decision.selected_upstream == "ollama"
        assert decision.selected_model == "llama3.3:70b"
        assert decision.selected_candidate_id == "ollama/llama3.3:70b"
        assert decision.selected_url == "http://localhost:11434/v1"
        assert decision.selected_slot_id is None
        assert "upstream_auto_selected" in decision.reason_codes
        assert any(w.startswith("auto_upstream_selected") for w in decision.warnings)

    def test_empty_fleet_selects_upstream_without_fallback_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv(AUTO_UPSTREAMS_ENV, "1")
        handler = _make_handler(tmp_path, yaml_content=_EMPTY_FLEET)
        decision = _handle(handler, task_type="chat")
        assert decision.selected_upstream == "ollama"
        assert decision.fallback_used is False
        assert "no_local_candidate" in decision.reason_codes

    def test_unhealthy_fleet_marks_fallback_used(self, tmp_path, monkeypatch):
        monkeypatch.setenv(AUTO_UPSTREAMS_ENV, "1")

        # Slots rank (health probe says healthy) but the failover walk sees
        # them unhealthy — the classic mid-flight death.
        from local_model_router.helpers.llama_cpp_manager import BackendManager

        BackendManager._instance = None
        cfg = tmp_path / "llama_cpp_servers.yaml"
        cfg.write_text(_FLEET_CONFIG)
        mgr = BackendManager(str(cfg))
        calls = {"n": 0}

        class FlakyChecker:
            async def check_async(self, config):
                calls["n"] += 1
                return "healthy" if calls["n"] <= 2 else "unhealthy"

            def check(self, config):
                return "unhealthy"

        mgr._health_checker = FlakyChecker()

        class StubObserver:
            def _make_manager(self):
                return mgr

        handler = RoutingIntentHandler(StubObserver(), upstream_rows_fn=lambda: _UPSTREAM_ROWS)
        decision = _handle(handler, task_type="chat")
        assert decision.selected_upstream == "ollama"
        assert decision.fallback_used is True
        assert "no_healthy_local_slot_upstream_fallback" in decision.reason_codes

    def test_local_only_never_routes_upstream(self, tmp_path, monkeypatch):
        monkeypatch.setenv(AUTO_UPSTREAMS_ENV, "1")
        handler = _make_handler(tmp_path, health="unhealthy")
        decision = _handle(handler, task_type="chat", local_only=True)
        assert decision.no_slot_available is True
        assert decision.selected_upstream is None

    def test_embedding_role_never_routes_upstream(self, tmp_path, monkeypatch):
        monkeypatch.setenv(AUTO_UPSTREAMS_ENV, "1")
        handler = _make_handler(tmp_path, health="unhealthy")
        decision = _handle(handler, task_type="embedding")
        assert decision.no_slot_available is True
        assert decision.selected_upstream is None

    def test_non_serving_upstream_never_considered(self, tmp_path, monkeypatch):
        monkeypatch.setenv(AUTO_UPSTREAMS_ENV, "1")
        rows = [row for row in _UPSTREAM_ROWS if row["name"] == "disabled_one"]
        handler = _make_handler(tmp_path, health="unhealthy", upstream_rows=rows)
        decision = _handle(handler, task_type="chat")
        assert decision.no_slot_available is True
        assert decision.selected_upstream is None

    def test_upstream_without_declared_models_never_considered(self, tmp_path, monkeypatch):
        monkeypatch.setenv(AUTO_UPSTREAMS_ENV, "1")
        rows = [dict(_UPSTREAM_ROWS[0], models=[])]
        handler = _make_handler(tmp_path, health="unhealthy", upstream_rows=rows)
        decision = _handle(handler, task_type="chat")
        assert decision.no_slot_available is True


# ---------------------------------------------------------------------------
# Catalog: upstream candidate construction and local-first ranking
# ---------------------------------------------------------------------------

class TestUpstreamCandidates:
    def test_build_upstream_candidates_shape(self):
        from local_model_router.routing.catalog import build_upstream_candidates

        candidates = build_upstream_candidates(_UPSTREAM_ROWS)
        assert [c.id for c in candidates] == ["ollama/llama3.3:70b"]
        cand = candidates[0]
        assert cand.source == "upstream"
        assert cand.upstream_name == "ollama"
        assert cand.model_id == "llama3.3:70b"
        assert cand.slot_id is None
        assert cand.supports_tools is True
        assert cand.supports_json_mode is False

    def test_healthy_local_outranks_upstream_in_every_strategy(self):
        from local_model_router.routing.catalog import (
            RoutingNeeds,
            build_slot_candidates,
            build_upstream_candidates,
            rank_candidates,
        )

        local = build_slot_candidates(
            [{"id": "chat", "role": "chat", "health": "healthy", "context_size": 8192}]
        )
        upstream = build_upstream_candidates(_UPSTREAM_ROWS)
        for strategy in ("balanced_local", "fastest", "quality", "economy"):
            ranked = rank_candidates(
                RoutingNeeds(role="chat", strategy=strategy), local + upstream
            )
            assert ranked[0].candidate.source == "local_fleet", strategy

    def test_local_only_filters_upstream_candidates(self):
        from local_model_router.routing.catalog import (
            RoutingNeeds,
            build_upstream_candidates,
            rank_candidates,
        )

        ranked = rank_candidates(
            RoutingNeeds(role="chat", local_only=True),
            build_upstream_candidates(_UPSTREAM_ROWS),
        )
        assert ranked == []


# ---------------------------------------------------------------------------
# Registry: declared models parsing
# ---------------------------------------------------------------------------

class TestRegistryModels:
    def test_models_parsed_and_described(self, tmp_path):
        from local_model_router.upstreams.registry import load_upstreams

        path = tmp_path / "upstreams.yaml"
        path.write_text(
            "upstreams:\n"
            "  - name: ollama\n"
            "    type: openai_compatible\n"
            "    base_url: http://localhost:11434/v1\n"
            "    enabled: true\n"
            "    models:\n"
            "      - llama3.3:70b\n"
            "      - '  '\n"
        )
        upstreams = load_upstreams(path)
        assert upstreams[0].models == ("llama3.3:70b",)
        assert upstreams[0].describe()["models"] == ["llama3.3:70b"]

    def test_models_default_empty(self, tmp_path):
        from local_model_router.upstreams.registry import load_upstreams

        path = tmp_path / "upstreams.yaml"
        path.write_text(
            "upstreams:\n"
            "  - name: ollama\n"
            "    type: openai_compatible\n"
            "    base_url: http://localhost:11434/v1\n"
            "    enabled: true\n"
        )
        assert load_upstreams(path)[0].models == ()

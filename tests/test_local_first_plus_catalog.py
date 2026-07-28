from __future__ import annotations

from local_model_router.routing.catalog import (
    ModelCandidate,
    RoutingNeeds,
    build_slot_candidates,
    rank_candidates,
    required_capabilities_from_chat_body,
)


def test_required_capabilities_from_chat_body_detects_tools_json_vision_and_tokens():
    needs = required_capabilities_from_chat_body(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                    ],
                }
            ],
            "tools": [{"type": "function", "function": {"name": "lookup"}}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "answer"}},
            "routing": {
                "estimated_tokens": 42000,
                "routing_strategy": "quality",
                "privacy_mode": "local_only",
            },
        },
        role="chat",
    )

    assert needs.requires_tools is True
    assert needs.requires_json_mode is True
    assert needs.requires_vision is True
    assert needs.estimated_tokens == 42000
    assert needs.strategy == "quality"
    assert needs.local_only is True


def test_rank_candidates_filters_by_capability_and_local_only_then_scores():
    needs = RoutingNeeds(
        role="chat",
        requires_tools=True,
        requires_json_mode=True,
        estimated_tokens=16000,
        local_only=True,
    )
    ranked = rank_candidates(
        needs,
        [
            ModelCandidate(
                id="chat",
                model_id="chat-model",
                source="local_fleet",
                role="chat",
                slot_id="chat",
                context_size=8192,
                supports_tools=False,
                supports_json_mode=True,
                quality_hint=0.9,
                latency_hint_ms=100,
            ),
            ModelCandidate(
                id="utility",
                model_id="utility-model",
                source="local_fleet",
                role="utility",
                slot_id="utility",
                context_size=32768,
                supports_tools=True,
                supports_json_mode=True,
                quality_hint=0.6,
                latency_hint_ms=30,
            ),
            ModelCandidate(
                id="ollama/remote",
                model_id="remote",
                source="upstream",
                role="chat",
                upstream_name="ollama",
                context_size=131072,
                supports_tools=True,
                supports_json_mode=True,
            ),
        ],
        strategy="balanced_local",
    )

    assert [item.candidate.id for item in ranked] == ["utility"]
    assert "capability_tools_match" in ranked[0].reason_codes
    assert ranked[0].score_inputs["context_size"] == 32768


def test_balanced_strategy_respects_role_and_preferred_slot_before_size_bias():
    candidates = [
        ModelCandidate(
            id="chat",
            model_id="chat-model",
            source="local_fleet",
            role="chat",
            slot_id="chat",
            context_size=65536,
            supports_json_mode=True,
            quality_hint=0.9,
            latency_hint_ms=180,
            health="healthy",
        ),
        ModelCandidate(
            id="utility",
            model_id="utility-model",
            source="local_fleet",
            role="utility",
            slot_id="utility",
            context_size=32768,
            supports_json_mode=True,
            quality_hint=0.55,
            latency_hint_ms=80,
            health="healthy",
        ),
    ]

    by_role = rank_candidates(RoutingNeeds(role="utility"), candidates)
    assert by_role[0].candidate.id == "utility"
    assert "role_matched" in by_role[0].reason_codes

    by_preferred = rank_candidates(RoutingNeeds(role="chat", preferred_slot="utility"), candidates)
    assert by_preferred[0].candidate.id == "utility"
    assert "preferred_slot_matched" in by_preferred[0].reason_codes


def test_build_slot_candidates_enriches_local_slot_metadata():
    candidates = build_slot_candidates(
        [
            {
                "id": "slot_router",
                "role": "chat",
                "model_id": "router",
                "base_url": "http://localhost:8080/v1",
                "context_size": 65536,
                "supports_tools": True,
                "supports_vision": False,
                "supports_json_mode": True,
                "quality_hint": 0.8,
                "latency_hint_ms": 75,
                "gpu_layers": -1,
                "health": "healthy",
            }
        ]
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.id == "slot_router"
    assert candidate.model_id == "router"
    assert candidate.source == "local_fleet"
    assert candidate.context_size == 65536
    assert candidate.supports_tools is True
    assert candidate.resource_cost_hint == 1.0


def test_evaluation_hints_feed_existing_ranker_without_second_score_formula():
    from local_model_router.routing.catalog import apply_evaluation_hints

    slots = [{
        "id": "chat",
        "model_id": "evaluated-model",
        "role": "chat",
        "enabled": True,
        "health": "healthy",
    }]
    snapshot = {"payload": {
        "schema_version": 1,
        "generated_at": "2026-07-17T00:00:00Z",
        "models": [{
            "model_id": "evaluated-model",
            "fingerprint": "abc",
            "roles": {"chat": {
                "pass_rate": 0.9,
                "reliability": 0.8,
                "median_latency_ms": 40,
                "resource_cost_hint": 0.35,
            }},
        }],
    }}

    ranked = rank_candidates(
        RoutingNeeds(role="chat", strategy="quality"),
        build_slot_candidates(apply_evaluation_hints(slots, snapshot)),
    )

    assert ranked[0].score_inputs["quality_hint"] == 0.9
    assert ranked[0].score_inputs["reliability"] == 0.8
    assert "evaluated_model_score" in ranked[0].reason_codes

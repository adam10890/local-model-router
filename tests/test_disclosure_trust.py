"""Executor trust resolution and the content-class / trust-tier matrix.

The matrix is the heart of the policy, so every cell is asserted rather than
sampled: seven content classes against four trust tiers.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_model_router.disclosure import (  # noqa: E402
    ALLOW,
    DENY,
    evaluate,
    find_upstream_executor,
    load_policy,
    resolve_agent,
    resolve_executor,
    resolve_slot,
    resolve_upstream,
)
from local_model_router.upstreams.registry import load_upstreams  # noqa: E402


@pytest.fixture()
def policy():
    return load_policy(None)


def _executor(policy, tier):
    return resolve_executor(policy, executor_id="target", config={"trust_tier": tier})


# ---------------------------------------------------------------------------
# Executor resolution
# ---------------------------------------------------------------------------

class TestExecutorResolution:
    def test_declared_tier_is_honored(self, policy):
        executor = _executor(policy, "private_cloud")
        assert executor.tier == "private_cloud"
        assert executor.declared is True

    def test_tier_is_case_insensitive(self, policy):
        assert _executor(policy, "Private_Cloud").tier == "private_cloud"

    def test_undeclared_executor_falls_to_least_trusted(self, policy):
        executor = resolve_executor(policy, executor_id="mystery", config=None)
        assert executor.tier == policy.default_executor_tier
        assert executor.declared is False

    def test_misspelled_tier_is_treated_as_undeclared(self, policy):
        # A typo must never invent a more trusted rung than the ladder has.
        executor = _executor(policy, "local_uncensoredd")
        assert executor.declared is False
        assert executor.tier == policy.default_executor_tier

    def test_empty_tier_string_is_undeclared(self, policy):
        assert _executor(policy, "").declared is False

    def test_resolve_slot_reads_slot_id_and_tier(self, policy):
        executor = resolve_slot(policy, {"id": "slot_chat", "trust_tier": "local_uncensored"})
        assert (executor.id, executor.kind, executor.tier) == (
            "slot_chat",
            "slot",
            "local_uncensored",
        )

    def test_resolve_agent_reads_agent_id(self, policy):
        executor = resolve_agent(policy, {"id": "code-review", "trust_tier": "local_aligned"})
        assert (executor.id, executor.kind) == ("code-review", "agent")

    def test_resolve_upstream_accepts_a_config_object(self, tmp_path, policy):
        path = tmp_path / "upstreams.yaml"
        path.write_text(
            "upstreams:\n"
            "  - name: declared\n"
            "    type: openai_compatible\n"
            "    base_url: http://x/v1\n"
            "    enabled: true\n"
            "    trust_tier: private_cloud\n",
            encoding="utf-8",
        )
        upstream = load_upstreams(path)[0]
        assert resolve_upstream(policy, upstream).tier == "private_cloud"

    def test_find_upstream_executor_matches_by_name(self, policy):
        rows = [{"name": "codex", "trust_tier": "private_cloud"}]
        assert find_upstream_executor(policy, rows, "codex").tier == "private_cloud"

    def test_find_upstream_executor_defaults_for_unknown_name(self, policy):
        executor = find_upstream_executor(policy, [{"name": "codex"}], "somethingelse")
        assert executor.declared is False
        assert executor.tier == policy.default_executor_tier


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------

_TIERS = ("local_uncensored", "local_aligned", "private_cloud", "other_provider")

# Expected outcome per content class, in _TIERS order.
_MATRIX = {
    "generic_scaffold":  (ALLOW, ALLOW, ALLOW, ALLOW),
    "algorithm_generic": (ALLOW, ALLOW, ALLOW, ALLOW),
    "product_feature":   (ALLOW, ALLOW, ALLOW, DENY),
    "integration_glue":  (ALLOW, ALLOW, ALLOW, DENY),
    "routing_policy":    (ALLOW, ALLOW, DENY,  DENY),
    "operator_data":     (ALLOW, DENY,  DENY,  DENY),
    "security_surface":  (ALLOW, DENY,  DENY,  DENY),
}


class TestMatrix:
    @pytest.mark.parametrize("content_class", sorted(_MATRIX))
    @pytest.mark.parametrize("index,tier", list(enumerate(_TIERS)))
    def test_every_cell(self, policy, content_class, index, tier):
        decision = evaluate(
            policy, executor=_executor(policy, tier), text="", declared_class=content_class
        )
        assert decision.outcome == _MATRIX[content_class][index]

    def test_matrix_covers_every_declared_content_class(self, policy):
        assert {c.id for c in policy.content_classes} == set(_MATRIX)

    def test_matrix_covers_every_declared_tier(self, policy):
        assert {t.id for t in policy.trust_tiers} == set(_TIERS)

    def test_denial_names_the_required_tier(self, policy):
        decision = evaluate(
            policy,
            executor=_executor(policy, "other_provider"),
            text="",
            declared_class="operator_data",
        )
        assert decision.outcome == DENY
        assert "executor_below_content_cap" in decision.reason_codes
        assert "requires_tier:local_uncensored" in decision.reason_codes

    def test_allow_says_why(self, policy):
        decision = evaluate(
            policy,
            executor=_executor(policy, "other_provider"),
            text="",
            declared_class="generic_scaffold",
        )
        assert "executor_within_content_cap" in decision.reason_codes
        assert "required_form:skeleton_only" in decision.reason_codes

    def test_undeclared_executor_is_flagged_in_reasons(self, policy):
        decision = evaluate(
            policy,
            executor=resolve_executor(policy, executor_id="x", config=None),
            text="",
            declared_class="generic_scaffold",
        )
        assert "executor_tier_undeclared_defaulted" in decision.reason_codes


# ---------------------------------------------------------------------------
# Forbidden content overrides an otherwise-permitted cell
# ---------------------------------------------------------------------------

class TestForbiddenContentOverride:
    def test_secret_blocks_an_otherwise_allowed_forward(self, policy):
        decision = evaluate(
            policy,
            executor=_executor(policy, "other_provider"),
            text="Write a boilerplate parser.\napi_key: leaked-value\n",
            declared_class="generic_scaffold",
        )
        assert decision.outcome == DENY
        assert "forbidden_content_detected" in decision.reason_codes
        assert "pattern:assigned_secret" in decision.reason_codes

    def test_most_trusted_executor_may_still_see_forbidden_content(self, policy):
        decision = evaluate(
            policy,
            executor=_executor(policy, "local_uncensored"),
            text="api_key: leaked-value",
            declared_class="operator_data",
        )
        assert decision.outcome == ALLOW

    def test_low_severity_findings_are_noted_not_blocking(self, policy):
        decision = evaluate(
            policy,
            executor=_executor(policy, "other_provider"),
            text="Write a boilerplate parser for an RTX 4090 box.",
            declared_class="generic_scaffold",
        )
        assert decision.outcome == ALLOW
        assert "forbidden_content_noted" in decision.reason_codes

    def test_decision_describe_never_quotes_content(self, policy):
        import json

        secret = "sk-abcdefghijklmnopqrstuvwx"
        decision = evaluate(
            policy,
            executor=_executor(policy, "other_provider"),
            text=f"api_key: {secret}",
            declared_class="generic_scaffold",
        )
        assert secret not in json.dumps(decision.describe())


# ---------------------------------------------------------------------------
# Shipped configuration
# ---------------------------------------------------------------------------

class TestShippedConfig:
    def test_no_shipped_upstream_claims_private_cloud_without_evidence(self, policy):
        # private_cloud is a claim about a contract, not a default.
        for upstream in load_upstreams(REPO_ROOT / "conf" / "upstreams.yaml"):
            assert upstream.trust_tier != "private_cloud"

    def test_every_shipped_upstream_declares_a_known_tier(self, policy):
        for upstream in load_upstreams(REPO_ROOT / "conf" / "upstreams.yaml"):
            assert policy.tier(upstream.trust_tier) is not None, upstream.name

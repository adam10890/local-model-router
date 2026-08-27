"""Disclosure rules: loading, validation, classification, and scanning.

Hermetic: the packaged rules plus in-memory overrides. No fleet, no network.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_model_router.disclosure import (  # noqa: E402
    DisclosureConfigError,
    classify,
    load_policy,
    parse_policy,
    scan_text,
    template_for,
    validate,
)
from local_model_router.disclosure.brief import declared_class_of, sections_in  # noqa: E402


@pytest.fixture()
def policy():
    return load_policy(None)


def _packaged_rules() -> dict:
    text = (
        REPO_ROOT / "local_model_router" / "disclosure" / "disclosure.yaml"
    ).read_text(encoding="utf-8")
    return yaml.safe_load(text)


# ---------------------------------------------------------------------------
# Packaged rules
# ---------------------------------------------------------------------------

class TestPackagedRules:
    def test_trust_ladder_is_ordered_most_trusted_first(self, policy):
        assert [t.id for t in policy.trust_tiers] == [
            "local_uncensored",
            "local_aligned",
            "private_cloud",
            "other_provider",
        ]
        assert [t.rank for t in policy.trust_tiers] == sorted(t.rank for t in policy.trust_tiers)

    def test_undeclared_executors_default_to_least_trusted(self, policy):
        least = max(policy.trust_tiers, key=lambda t: t.rank)
        assert policy.default_executor_tier == least.id

    def test_every_content_class_names_a_known_tier_and_form(self, policy):
        tiers = {t.id for t in policy.trust_tiers}
        for item in policy.content_classes:
            assert item.max_executor_tier in tiers
            assert item.form in policy.forms

    def test_operator_and_security_content_never_leaves_the_machine(self, policy):
        local_rank = policy.rank_of("local_uncensored")
        for class_id in ("operator_data", "security_surface"):
            found = policy.content_class(class_id)
            assert policy.rank_of(found.max_executor_tier) == local_rank

    def test_describe_never_exposes_pattern_source(self, policy):
        import json

        blob = json.dumps(policy.describe())
        for fragment in ("BEGIN ", "A-Za-z0-9", "(?i)", "ghp_"):
            assert fragment not in blob
        assert set(policy.describe()["forbidden_patterns"][0]) == {"id", "severity"}


# ---------------------------------------------------------------------------
# Validation of overrides
# ---------------------------------------------------------------------------

class TestPolicyValidation:
    def test_override_file_is_used_when_present(self, tmp_path, policy):
        rules = _packaged_rules()
        rules["default_executor_tier"] = "private_cloud"
        path = tmp_path / "disclosure.yaml"
        path.write_text(yaml.safe_dump(rules), encoding="utf-8")
        loaded = load_policy(path)
        assert loaded.default_executor_tier == "private_cloud"
        assert loaded.source == str(path)

    def test_missing_override_falls_back_to_packaged(self, tmp_path):
        loaded = load_policy(tmp_path / "does-not-exist.yaml")
        assert loaded.source == "packaged"

    def test_unknown_tier_in_content_class_is_rejected(self):
        rules = _packaged_rules()
        rules["content_classes"][0]["max_executor_tier"] = "imaginary_tier"
        with pytest.raises(DisclosureConfigError):
            parse_policy(rules)

    def test_unknown_default_executor_tier_is_rejected(self):
        rules = _packaged_rules()
        rules["default_executor_tier"] = "nope"
        with pytest.raises(DisclosureConfigError):
            parse_policy(rules)

    def test_duplicate_content_class_is_rejected(self):
        rules = _packaged_rules()
        rules["content_classes"].append(dict(rules["content_classes"][0]))
        with pytest.raises(DisclosureConfigError):
            parse_policy(rules)

    def test_invalid_regex_is_rejected(self):
        rules = _packaged_rules()
        rules["forbidden_patterns"][0]["pattern"] = "([unclosed"
        with pytest.raises(DisclosureConfigError):
            parse_policy(rules)

    def test_malformed_override_raises_rather_than_silently_widening(self, tmp_path):
        path = tmp_path / "disclosure.yaml"
        path.write_text("trust_tiers: []\n", encoding="utf-8")
        with pytest.raises(DisclosureConfigError):
            load_policy(path)

    def test_unknown_tier_ranks_below_every_declared_tier(self, policy):
        assert policy.rank_of("not-a-tier") > max(t.rank for t in policy.trust_tiers)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

class TestClassifier:
    def test_declared_class_wins(self, policy):
        result = classify(policy, "boilerplate parser", declared_class="operator_data")
        assert result.content_class == "operator_data"
        assert result.declared is True

    def test_unknown_declared_class_falls_back_to_default(self, policy):
        result = classify(policy, "anything", declared_class="not_a_class")
        assert result.content_class == policy.default_content_class
        assert "declared_content_class_unknown" in result.reason_codes

    def test_keyword_inference(self, policy):
        result = classify(policy, "Write a boilerplate parser stub.")
        assert result.content_class == "generic_scaffold"
        assert "boilerplate" in result.matched_keywords

    def test_most_restrictive_match_wins(self, policy):
        # Mentions both a scaffold and a credential: the security cap must win.
        result = classify(policy, "Add a boilerplate adapter that stores an api key.")
        assert result.content_class == "security_surface"
        assert "most_restrictive_match_won" in result.reason_codes

    def test_empty_text_uses_default_class(self, policy):
        result = classify(policy, "   ")
        assert result.content_class == policy.default_content_class
        assert "empty_content" in result.reason_codes

    def test_no_keyword_match_uses_default_class(self, policy):
        result = classify(policy, "zzzz qqqq wwww")
        assert result.content_class == policy.default_content_class
        assert "no_keyword_match" in result.reason_codes

    def test_keywords_match_whole_words_only(self, policy):
        result = classify(policy, "The scaffolding around the building collapsed.")
        assert "scaffold" not in result.matched_keywords

    def test_classification_is_stable(self, policy):
        text = "Design a caching algorithm and a boilerplate adapter."
        assert classify(policy, text).content_class == classify(policy, text).content_class


# ---------------------------------------------------------------------------
# Scanner — the no-echo invariant
# ---------------------------------------------------------------------------

class TestScanner:
    def test_clean_text_has_no_findings(self, policy):
        result = scan_text(policy, "Parse records and return normalized rows.")
        assert result.clean is True
        assert result.highest_severity == "none"

    def test_empty_text_is_clean(self, policy):
        assert scan_text(policy, "").clean is True

    @pytest.mark.parametrize(
        "text,pattern_id",
        [
            ("api_key: abc123xyz", "assigned_secret"),
            ("Authorization: Bearer aaaaaaaaaaaaaaaaaaaa", "bearer_token"),
            ("-----BEGIN RSA PRIVATE KEY-----", "private_key_block"),
            (r"C:\Users\someone\models", "windows_user_path"),
            ("/home/someone/models/", "posix_home_path"),
            ("https://api.example.com/v1", "routable_host"),
            ("Model-Name-Q8_0.gguf", "gguf_model_file"),
            ("an RTX 4090 box", "hardware_identity"),
            ("so that we can win the deal", "purpose_leak"),
        ],
    )
    def test_forbidden_patterns_are_detected(self, policy, text, pattern_id):
        assert pattern_id in scan_text(policy, text).pattern_ids()

    def test_loopback_urls_are_not_flagged(self, policy):
        assert scan_text(policy, "http://127.0.0.1:8080/v1 and localhost:9000").clean is True

    def test_findings_never_carry_matched_text(self, policy):
        import json

        secret = "sk-abcdefghijklmnopqrstuvwx"
        result = scan_text(policy, f"api_key: {secret}\n/home/someone/x/\n")
        assert not result.clean
        blob = json.dumps(result.describe())
        assert secret not in blob
        assert "someone" not in blob
        for finding in result.findings:
            assert set(finding.describe()) == {"pattern_id", "severity", "count", "lines"}

    def test_line_numbers_are_one_based(self, policy):
        result = scan_text(policy, "clean line\napi_key: value\n")
        finding = next(f for f in result.findings if f.pattern_id == "assigned_secret")
        assert finding.lines == (2,)

    def test_repeat_occurrences_are_counted(self, policy):
        result = scan_text(policy, "api_key: one\napi_key: two\n")
        finding = next(f for f in result.findings if f.pattern_id == "assigned_secret")
        assert finding.count == 2
        assert finding.lines == (1, 2)

    def test_highest_severity_is_the_most_severe_present(self, policy):
        result = scan_text(policy, "an RTX 4090 box\napi_key: value\n")
        assert result.highest_severity == "critical"


# ---------------------------------------------------------------------------
# Briefs
# ---------------------------------------------------------------------------

class TestBriefs:
    def test_template_covers_every_content_class(self, policy):
        for item in policy.content_classes:
            rendered = template_for(policy, item.id)
            assert f"content_class: {item.id}" in rendered
            for section in policy.required_sections(item.form):
                assert f"## {section}" in rendered

    def test_template_rejects_unknown_class(self, policy):
        with pytest.raises(ValueError):
            template_for(policy, "not_a_class")

    def test_template_declares_its_own_class(self, policy):
        rendered = template_for(policy, "routing_policy")
        assert declared_class_of(rendered) == "routing_policy"

    def test_declared_class_is_only_read_from_the_header(self, policy):
        body = "# Title\n\ncontent_class: operator_data\n"
        assert declared_class_of(body) is None

    def test_sections_are_normalized(self):
        assert sections_in("## Added Locally\n### requirements\n") == (
            "added_locally",
            "requirements",
        )

    def test_missing_sections_are_reported(self, policy):
        brief = "content_class: generic_scaffold\n\n## requirements\n\nDo the thing.\n"
        result = validate(policy, brief)
        assert result.valid is False
        assert "interface" in result.missing_sections
        assert "missing_section:interface" in result.reason_codes

    def test_complete_clean_brief_is_valid(self, policy):
        brief = (
            "content_class: generic_scaffold\n\n"
            "## requirements\nParse a record stream.\n\n"
            "## interface\nparse(stream) -> rows\n\n"
            "## acceptance\nMalformed rows raise ParseError.\n\n"
            "## added_locally\nReal field names.\n"
        )
        result = validate(policy, brief)
        assert result.valid is True
        assert "brief_valid" in result.reason_codes

    def test_forbidden_content_invalidates_a_complete_brief(self, policy):
        brief = (
            "content_class: generic_scaffold\n\n"
            "## requirements\nParse a record stream.\n\n"
            "## interface\nparse(stream) -> rows\n\n"
            "## acceptance\napi_key: leaked-value\n\n"
            "## added_locally\nReal field names.\n"
        )
        result = validate(policy, brief)
        assert result.valid is False
        assert "pattern:assigned_secret" in result.reason_codes

    def test_validation_output_never_quotes_the_brief(self, policy):
        import json

        secret = "sk-abcdefghijklmnopqrstuvwx"
        result = validate(policy, f"content_class: generic_scaffold\n\n## requirements\napi_key: {secret}\n")
        assert secret not in json.dumps(result.describe())

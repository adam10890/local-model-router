"""The disclosure matrix: may this content go to that executor, and in what form.

Two axes meet here. ``classifier`` says what the content is and the cap it
carries; ``trust`` says how far the executor is trusted. A handoff is allowed
when the executor is at or above the content's cap and the content carries no
forbidden pattern.

Every outcome is explainable: the result carries reason codes in the same
style as the routing decision path, so a denial is never silent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .classifier import Classification, classify
from .policy import DisclosurePolicy
from .scanner import ScanResult, scan_text
from .trust import Executor

ALLOW = "allow"
DENY = "deny"

_BLOCKING_SEVERITIES = frozenset({"critical", "high"})


@dataclass(frozen=True)
class DisclosureDecision:
    """Outcome of one content-to-executor evaluation."""

    outcome: str
    content_class: str
    required_form: str
    max_executor_tier: str
    executor_id: str
    executor_tier: str
    executor_declared: bool
    scan: ScanResult
    reason_codes: Tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return self.outcome == ALLOW

    def describe(self) -> Dict[str, Any]:
        """Safe public description — carries no scanned text."""
        return {
            "outcome": self.outcome,
            "content_class": self.content_class,
            "required_form": self.required_form,
            "max_executor_tier": self.max_executor_tier,
            "executor": {
                "id": self.executor_id,
                "trust_tier": self.executor_tier,
                "declared": self.executor_declared,
            },
            "scan": self.scan.describe(),
            "reason_codes": list(self.reason_codes),
        }


def evaluate(
    policy: DisclosurePolicy,
    *,
    executor: Executor,
    text: str = "",
    declared_class: Optional[str] = None,
    classification: Optional[Classification] = None,
    scan: Optional[ScanResult] = None,
) -> DisclosureDecision:
    """Evaluate one handoff.

    *text* is read to classify and scan; it is never stored on the result.
    Callers that already classified or scanned may pass those in to avoid
    re-reading the body.
    """
    resolved = classification or classify(policy, text, declared_class=declared_class)
    result = scan if scan is not None else scan_text(policy, text)

    reasons = list(resolved.reason_codes)
    reasons.append(f"content_class:{resolved.content_class}")
    reasons.append(f"executor_tier:{executor.tier}")
    if not executor.declared:
        reasons.append("executor_tier_undeclared_defaulted")

    outcome = ALLOW
    allowed_rank = policy.rank_of(resolved.max_executor_tier)
    if executor.rank > allowed_rank:
        outcome = DENY
        reasons.append("executor_below_content_cap")
        reasons.append(f"requires_tier:{resolved.max_executor_tier}")
    else:
        reasons.append("executor_within_content_cap")

    # Forbidden content blocks every executor except the most trusted rung,
    # which is the only one allowed to see content in its `full` form.
    most_trusted_rank = policy.trust_tiers[0].rank if policy.trust_tiers else 0
    blocking = tuple(f for f in result.findings if f.severity in _BLOCKING_SEVERITIES)
    if blocking and executor.rank > most_trusted_rank:
        outcome = DENY
        reasons.append("forbidden_content_detected")
        reasons.extend(f"pattern:{finding.pattern_id}" for finding in blocking)
    elif result.findings:
        reasons.append("forbidden_content_noted")

    reasons.append(f"required_form:{resolved.form}")
    return DisclosureDecision(
        outcome=outcome,
        content_class=resolved.content_class,
        required_form=resolved.form,
        max_executor_tier=resolved.max_executor_tier,
        executor_id=executor.id,
        executor_tier=executor.tier,
        executor_declared=executor.declared,
        scan=result,
        reason_codes=tuple(reasons),
    )

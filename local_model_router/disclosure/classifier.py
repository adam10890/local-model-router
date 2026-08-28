"""Classify task content into a disclosure content class.

Deterministic keyword scoring — no model call, no network, no I/O. The
classifier is a *hint generator*: it reports the class, the evidence that
produced it, and its confidence, so a human or an explicit declaration can
override it. It never decides alone what may leave the machine; that is
``decision.evaluate``'s job.

Ties and empty input resolve to the policy's ``default_content_class``, which
is deliberately more restrictive than the most permissive class.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .policy import ContentClass, DisclosurePolicy


@dataclass(frozen=True)
class Classification:
    """A content-class hint with the evidence behind it."""

    content_class: str
    form: str
    max_executor_tier: str
    declared: bool
    matched_keywords: Tuple[str, ...]
    reason_codes: Tuple[str, ...]

    def describe(self) -> Dict[str, Any]:
        return {
            "content_class": self.content_class,
            "form": self.form,
            "max_executor_tier": self.max_executor_tier,
            "declared": self.declared,
            "matched_keywords": list(self.matched_keywords),
            "reason_codes": list(self.reason_codes),
        }


def _from_class(
    found: ContentClass, *, declared: bool, keywords: Tuple[str, ...], reasons: Tuple[str, ...]
) -> Classification:
    return Classification(
        content_class=found.id,
        form=found.form,
        max_executor_tier=found.max_executor_tier,
        declared=declared,
        matched_keywords=keywords,
        reason_codes=reasons,
    )


def _keyword_hits(text: str, keywords: Tuple[str, ...]) -> Tuple[str, ...]:
    hits = []
    for keyword in keywords:
        if not keyword:
            continue
        if re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text):
            hits.append(keyword)
    return tuple(hits)


def classify(
    policy: DisclosurePolicy, text: str, *, declared_class: Optional[str] = None
) -> Classification:
    """Classify *text*, honoring an explicit ``declared_class`` when valid.

    The most restrictive matching class wins, not the highest-scoring one: a
    brief that mentions both boilerplate and credentials is a security
    surface, never a generic scaffold.
    """
    if declared_class:
        found = policy.content_class(declared_class)
        if found is not None:
            return _from_class(
                found, declared=True, keywords=(), reasons=("content_class_declared",)
            )
        default = policy.content_class(policy.default_content_class)
        if default is not None:
            return _from_class(
                default,
                declared=False,
                keywords=(),
                reasons=("declared_content_class_unknown", "content_class_defaulted"),
            )

    body = (text or "").lower()
    if not body.strip():
        default = policy.content_class(policy.default_content_class)
        if default is None:  # pragma: no cover - policy validation forbids this
            raise ValueError("policy has no default content class")
        return _from_class(
            default, declared=False, keywords=(), reasons=("empty_content", "content_class_defaulted")
        )

    matches: list[tuple[int, ContentClass, Tuple[str, ...]]] = []
    for candidate in policy.content_classes:
        hits = _keyword_hits(body, candidate.keywords)
        if hits:
            matches.append((policy.rank_of(candidate.max_executor_tier), candidate, hits))

    if not matches:
        default = policy.content_class(policy.default_content_class)
        if default is None:  # pragma: no cover - policy validation forbids this
            raise ValueError("policy has no default content class")
        return _from_class(
            default,
            declared=False,
            keywords=(),
            reasons=("no_keyword_match", "content_class_defaulted"),
        )

    # Lowest executor rank == most restrictive cap. Break ties by hit count,
    # then by class id so the result is stable across runs.
    matches.sort(key=lambda item: (item[0], -len(item[2]), item[1].id))
    _, winner, hits = matches[0]
    reasons = ["content_class_inferred"]
    if len(matches) > 1:
        reasons.append("most_restrictive_match_won")
    return _from_class(winner, declared=False, keywords=hits, reasons=tuple(reasons))

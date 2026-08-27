"""Task disclosure: what may be handed to which executor, and in what form.

Imperium brokers tasks between agents and harnesses. Every handoff is a
disclosure decision on two axes — how sensitive the content is, and how far
the executor is trusted. This package owns both axes and the matrix between
them.
"""
from __future__ import annotations

from .brief import BriefValidation, template_for, validate
from .classifier import Classification, classify
from .decision import ALLOW, DENY, DisclosureDecision, evaluate
from .policy import (
    CONFIG_FILENAME,
    ContentClass,
    DisclosureConfigError,
    DisclosurePolicy,
    TrustTier,
    load_policy,
    parse_policy,
)
from .scanner import Finding, ScanResult, scan_text
from .trust import (
    Executor,
    find_upstream_executor,
    resolve_agent,
    resolve_executor,
    resolve_slot,
    resolve_upstream,
)

__all__ = [
    "ALLOW",
    "DENY",
    "BriefValidation",
    "Classification",
    "ContentClass",
    "CONFIG_FILENAME",
    "DisclosureConfigError",
    "DisclosureDecision",
    "DisclosurePolicy",
    "Executor",
    "Finding",
    "ScanResult",
    "TrustTier",
    "classify",
    "evaluate",
    "find_upstream_executor",
    "load_policy",
    "parse_policy",
    "resolve_agent",
    "resolve_executor",
    "resolve_slot",
    "resolve_upstream",
    "scan_text",
    "template_for",
    "validate",
]

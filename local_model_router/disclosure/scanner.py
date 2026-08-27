"""Scan text for content that must not leave for a lower-trust executor.

Hard invariant: a finding carries the pattern id, severity, occurrence count,
and line numbers — **never the matched text and never surrounding context**.
A scanner that echoed what it found would itself become the leak it exists to
prevent, and its output is meant to be safe for headers, telemetry, and CLI
output alike.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from .policy import DisclosurePolicy

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_MAX_REPORTED_LINES = 20


@dataclass(frozen=True)
class Finding:
    """One forbidden pattern seen in scanned text. Carries no matched text."""

    pattern_id: str
    severity: str
    count: int
    lines: Tuple[int, ...]

    def describe(self) -> Dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "severity": self.severity,
            "count": self.count,
            "lines": list(self.lines),
        }


@dataclass(frozen=True)
class ScanResult:
    """Aggregate scan outcome. Safe to log, serialize, and return over HTTP."""

    findings: Tuple[Finding, ...]

    @property
    def clean(self) -> bool:
        return not self.findings

    @property
    def highest_severity(self) -> str:
        if not self.findings:
            return "none"
        return min(
            (f.severity for f in self.findings),
            key=lambda s: _SEVERITY_ORDER.get(s, len(_SEVERITY_ORDER)),
        )

    def pattern_ids(self) -> Tuple[str, ...]:
        return tuple(f.pattern_id for f in self.findings)

    def describe(self) -> Dict[str, Any]:
        return {
            "clean": self.clean,
            "highest_severity": self.highest_severity,
            "findings": [f.describe() for f in self.findings],
        }


def scan_text(policy: DisclosurePolicy, text: str) -> ScanResult:
    """Return the forbidden patterns present in *text*, without quoting it."""
    body = text or ""
    if not body:
        return ScanResult(findings=())

    line_starts = [0]
    for index, character in enumerate(body):
        if character == "\n":
            line_starts.append(index + 1)

    def line_of(offset: int) -> int:
        low, high = 0, len(line_starts) - 1
        while low < high:
            mid = (low + high + 1) // 2
            if line_starts[mid] <= offset:
                low = mid
            else:
                high = mid - 1
        return low + 1

    findings: List[Finding] = []
    for pattern in policy.forbidden_patterns:
        lines: List[int] = []
        count = 0
        for match in pattern.regex.finditer(body):
            count += 1
            line = line_of(match.start())
            if line not in lines and len(lines) < _MAX_REPORTED_LINES:
                lines.append(line)
        if count:
            findings.append(
                Finding(
                    pattern_id=pattern.id,
                    severity=pattern.severity,
                    count=count,
                    lines=tuple(lines),
                )
            )

    findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.pattern_id))
    return ScanResult(findings=tuple(findings))

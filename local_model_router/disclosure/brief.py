"""Task-brief templates and validation.

A *brief* is what an executor actually receives. Its form is fixed by the
content class: a `skeleton_only` brief states requirements and interface
shapes and nothing else; a `requirements_only` brief drops the interface but
still never explains why the product wants the behavior.

Sections are declared as ``## <name>`` headings so a brief stays a plain
Markdown file the operator can edit in any editor.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .classifier import Classification, classify
from .policy import DisclosurePolicy
from .scanner import ScanResult, scan_text

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<name>.+?)\s*$", re.MULTILINE)
_FRONT_MATTER = re.compile(r"^\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<value>\S.*?)\s*$")

_SECTION_PROMPTS = {
    "requirements": "What must be true when this is done. Behavior only.",
    "interface": "Function, module, or endpoint shapes. Placeholder names only.",
    "context": "Minimum internal context. Replace every identifier with a placeholder.",
    "acceptance": "How the result is checked. Concrete, verifiable statements.",
    "added_locally": "What we attach after delivery: real names, data, and wiring.",
}


@dataclass(frozen=True)
class BriefValidation:
    """Result of checking a brief against its required sections."""

    content_class: str
    required_form: str
    present_sections: Tuple[str, ...]
    missing_sections: Tuple[str, ...]
    scan: ScanResult
    reason_codes: Tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.missing_sections and self.scan.clean

    def describe(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "content_class": self.content_class,
            "required_form": self.required_form,
            "present_sections": list(self.present_sections),
            "missing_sections": list(self.missing_sections),
            "scan": self.scan.describe(),
            "reason_codes": list(self.reason_codes),
        }


def _normalize_section(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")


def declared_class_of(text: str) -> Optional[str]:
    """Read an explicit ``content_class:`` declaration from a brief's header.

    Only the lines before the first heading are inspected, so the word can
    still appear in prose without changing the brief's class.
    """
    head = _HEADING.split(text or "", maxsplit=1)[0]
    for line in head.splitlines():
        match = _FRONT_MATTER.match(line)
        if match and _normalize_section(match.group("key")) == "content_class":
            return match.group("value").strip().lower()
    return None


def sections_in(text: str) -> Tuple[str, ...]:
    """Return the normalized section names present in *text*."""
    seen: List[str] = []
    for match in _HEADING.finditer(text or ""):
        name = _normalize_section(match.group("name"))
        if name and name not in seen:
            seen.append(name)
    return tuple(seen)


def template_for(policy: DisclosurePolicy, content_class: str) -> str:
    """Render an empty brief skeleton for *content_class*."""
    found = policy.content_class(content_class)
    if found is None:
        known = ", ".join(c.id for c in policy.content_classes)
        raise ValueError(f"unknown content class '{content_class}'; known: {known}")

    lines = [
        f"content_class: {found.id}",
        f"# Task brief ({found.form})",
        "",
        f"Class: {found.label or found.id}. "
        f"May be sent to executors at or above trust tier `{found.max_executor_tier}`.",
        "",
        "Do not state why the product wants this. Requirements only, placeholder",
        "names only, synthetic examples only.",
        "",
    ]
    for section in policy.required_sections(found.form) or ("requirements",):
        lines.append(f"## {section}")
        lines.append("")
        prompt = _SECTION_PROMPTS.get(section)
        if prompt:
            lines.append(f"<!-- {prompt} -->")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def validate(
    policy: DisclosurePolicy, text: str, *, declared_class: Optional[str] = None
) -> BriefValidation:
    """Check a brief's required sections and scan it for forbidden content."""
    declared = declared_class or declared_class_of(text)
    classification: Classification = classify(policy, text, declared_class=declared)
    present = sections_in(text)
    required = policy.required_sections(classification.form)
    missing = tuple(section for section in required if section not in present)
    result = scan_text(policy, text)

    reasons = list(classification.reason_codes)
    reasons.append(f"content_class:{classification.content_class}")
    reasons.append(f"required_form:{classification.form}")
    if missing:
        reasons.extend(f"missing_section:{section}" for section in missing)
    if not result.clean:
        reasons.extend(f"pattern:{pattern_id}" for pattern_id in result.pattern_ids())
    if not missing and result.clean:
        reasons.append("brief_valid")

    return BriefValidation(
        content_class=classification.content_class,
        required_form=classification.form,
        present_sections=present,
        missing_sections=missing,
        scan=result,
        reason_codes=tuple(reasons),
    )

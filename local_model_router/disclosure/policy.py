"""Task disclosure rules: load, validate, and expose them safely.

The packaged ``disclosure.yaml`` is the immutable default. An operator may
override it with ``conf/disclosure.yaml`` next to ``apps.yaml`` — the same
override shape the agent catalog uses.

Nothing here reads prompt bodies, touches the network, or knows about
Starlette.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

CONFIG_FILENAME = "disclosure.yaml"


class DisclosureConfigError(ValueError):
    """Raised when the disclosure rules file is invalid."""


@dataclass(frozen=True)
class TrustTier:
    """One rung of the executor trust ladder. Lower rank is more trusted."""

    id: str
    rank: int
    label: str = ""

    def describe(self) -> Dict[str, Any]:
        return {"id": self.id, "rank": self.rank, "label": self.label}


@dataclass(frozen=True)
class ContentClass:
    """A class of task content, with the cap it places on executors."""

    id: str
    label: str
    max_executor_tier: str
    form: str
    keywords: Tuple[str, ...] = ()

    def describe(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "max_executor_tier": self.max_executor_tier,
            "form": self.form,
            "keywords": list(self.keywords),
        }


@dataclass(frozen=True)
class ForbiddenPattern:
    """A pattern that must not reach an executor below the content cap.

    ``regex`` is compiled once. Callers report ``id`` and location only —
    the matched text is never carried out of the scanner.
    """

    id: str
    regex: re.Pattern[str]
    severity: str = "medium"

    def describe(self) -> Dict[str, Any]:
        return {"id": self.id, "severity": self.severity}


@dataclass(frozen=True)
class DisclosurePolicy:
    """Validated disclosure rules."""

    trust_tiers: Tuple[TrustTier, ...]
    content_classes: Tuple[ContentClass, ...]
    forbidden_patterns: Tuple[ForbiddenPattern, ...]
    forms: Tuple[str, ...]
    default_executor_tier: str
    default_content_class: str
    brief_required_sections: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    source: str = "packaged"

    # ── lookups ─────────────────────────────────────────────────────────
    def tier(self, tier_id: str) -> Optional[TrustTier]:
        key = str(tier_id or "").strip().lower()
        return next((t for t in self.trust_tiers if t.id == key), None)

    def content_class(self, class_id: str) -> Optional[ContentClass]:
        key = str(class_id or "").strip().lower()
        return next((c for c in self.content_classes if c.id == key), None)

    def rank_of(self, tier_id: str) -> int:
        """Trust rank of a tier; unknown tiers rank as the least trusted."""
        found = self.tier(tier_id)
        if found is not None:
            return found.rank
        return max((t.rank for t in self.trust_tiers), default=0) + 1

    def required_sections(self, form: str) -> Tuple[str, ...]:
        return self.brief_required_sections.get(str(form or "").strip().lower(), ())

    def describe(self) -> Dict[str, Any]:
        """Safe public description. Never includes pattern source text."""
        return {
            "source": self.source,
            "trust_tiers": [t.describe() for t in self.trust_tiers],
            "content_classes": [c.describe() for c in self.content_classes],
            "forms": list(self.forms),
            "default_executor_tier": self.default_executor_tier,
            "default_content_class": self.default_content_class,
            "forbidden_patterns": [p.describe() for p in self.forbidden_patterns],
            "brief_required_sections": {
                form: list(sections) for form, sections in self.brief_required_sections.items()
            },
        }


def _require_list(data: Dict[str, Any], key: str) -> List[Any]:
    value = data.get(key)
    if not isinstance(value, list) or not value:
        raise DisclosureConfigError(f"'{key}' must be a non-empty list")
    return value


def _normalized_id(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        raise DisclosureConfigError(f"'{field_name}' must not be empty")
    return text


def _parse_trust_tiers(raw_tiers: List[Any]) -> Tuple[TrustTier, ...]:
    tiers: List[TrustTier] = []
    seen: set[str] = set()
    for raw in raw_tiers:
        if not isinstance(raw, dict):
            raise DisclosureConfigError("each trust tier must be a mapping")
        tier_id = _normalized_id(raw.get("id"), field_name="trust_tiers[].id")
        if tier_id in seen:
            raise DisclosureConfigError(f"duplicate trust tier: {tier_id}")
        try:
            rank = int(raw.get("rank"))
        except (TypeError, ValueError) as exc:
            raise DisclosureConfigError(f"trust tier '{tier_id}' needs an integer rank") from exc
        seen.add(tier_id)
        tiers.append(TrustTier(id=tier_id, rank=rank, label=str(raw.get("label") or "")))
    if len({t.rank for t in tiers}) != len(tiers):
        raise DisclosureConfigError("trust tier ranks must be unique")
    return tuple(sorted(tiers, key=lambda t: t.rank))


def _parse_content_classes(
    raw_classes: List[Any], tier_ids: set[str], form_ids: set[str]
) -> Tuple[ContentClass, ...]:
    classes: List[ContentClass] = []
    seen: set[str] = set()
    for raw in raw_classes:
        if not isinstance(raw, dict):
            raise DisclosureConfigError("each content class must be a mapping")
        class_id = _normalized_id(raw.get("id"), field_name="content_classes[].id")
        if class_id in seen:
            raise DisclosureConfigError(f"duplicate content class: {class_id}")
        max_tier = _normalized_id(
            raw.get("max_executor_tier"), field_name=f"{class_id}.max_executor_tier"
        )
        if max_tier not in tier_ids:
            raise DisclosureConfigError(
                f"content class '{class_id}' names unknown trust tier '{max_tier}'"
            )
        form = _normalized_id(raw.get("form"), field_name=f"{class_id}.form")
        if form not in form_ids:
            raise DisclosureConfigError(f"content class '{class_id}' names unknown form '{form}'")
        keywords = raw.get("keywords")
        keyword_tuple = (
            tuple(str(k).strip().lower() for k in keywords if str(k).strip())
            if isinstance(keywords, list)
            else ()
        )
        seen.add(class_id)
        classes.append(
            ContentClass(
                id=class_id,
                label=str(raw.get("label") or ""),
                max_executor_tier=max_tier,
                form=form,
                keywords=keyword_tuple,
            )
        )
    return tuple(classes)


def _parse_forbidden_patterns(raw_patterns: List[Any]) -> Tuple[ForbiddenPattern, ...]:
    patterns: List[ForbiddenPattern] = []
    seen: set[str] = set()
    for raw in raw_patterns:
        if not isinstance(raw, dict):
            raise DisclosureConfigError("each forbidden pattern must be a mapping")
        pattern_id = _normalized_id(raw.get("id"), field_name="forbidden_patterns[].id")
        if pattern_id in seen:
            raise DisclosureConfigError(f"duplicate forbidden pattern: {pattern_id}")
        source = str(raw.get("pattern") or "")
        if not source:
            raise DisclosureConfigError(f"forbidden pattern '{pattern_id}' has no pattern")
        try:
            compiled = re.compile(source)
        except re.error as exc:
            raise DisclosureConfigError(
                f"forbidden pattern '{pattern_id}' is not a valid regular expression: {exc}"
            ) from exc
        seen.add(pattern_id)
        patterns.append(
            ForbiddenPattern(
                id=pattern_id,
                regex=compiled,
                severity=str(raw.get("severity") or "medium").strip().lower(),
            )
        )
    return tuple(patterns)


def parse_policy(data: Any, *, source: str = "packaged") -> DisclosurePolicy:
    """Validate a rules mapping into a DisclosurePolicy."""
    if not isinstance(data, dict):
        raise DisclosureConfigError("disclosure rules must be a mapping")

    tiers = _parse_trust_tiers(_require_list(data, "trust_tiers"))
    tier_ids = {t.id for t in tiers}

    raw_forms = _require_list(data, "forms")
    forms: List[str] = []
    for raw in raw_forms:
        if not isinstance(raw, dict):
            raise DisclosureConfigError("each form must be a mapping")
        forms.append(_normalized_id(raw.get("id"), field_name="forms[].id"))
    if len(set(forms)) != len(forms):
        raise DisclosureConfigError("form ids must be unique")

    classes = _parse_content_classes(_require_list(data, "content_classes"), tier_ids, set(forms))
    patterns = _parse_forbidden_patterns(_require_list(data, "forbidden_patterns"))

    default_tier = _normalized_id(
        data.get("default_executor_tier"), field_name="default_executor_tier"
    )
    if default_tier not in tier_ids:
        raise DisclosureConfigError(f"default_executor_tier '{default_tier}' is not a trust tier")

    default_class = _normalized_id(
        data.get("default_content_class"), field_name="default_content_class"
    )
    if default_class not in {c.id for c in classes}:
        raise DisclosureConfigError(
            f"default_content_class '{default_class}' is not a content class"
        )

    raw_sections = data.get("brief_required_sections")
    sections: Dict[str, Tuple[str, ...]] = {}
    if isinstance(raw_sections, dict):
        for form, values in raw_sections.items():
            form_id = _normalized_id(form, field_name="brief_required_sections key")
            if form_id not in set(forms):
                raise DisclosureConfigError(
                    f"brief_required_sections names unknown form '{form_id}'"
                )
            if not isinstance(values, list):
                raise DisclosureConfigError(f"brief_required_sections['{form_id}'] must be a list")
            sections[form_id] = tuple(str(v).strip().lower() for v in values if str(v).strip())

    return DisclosurePolicy(
        trust_tiers=tiers,
        content_classes=classes,
        forbidden_patterns=patterns,
        forms=tuple(forms),
        default_executor_tier=default_tier,
        default_content_class=default_class,
        brief_required_sections=sections,
        source=source,
    )


def _packaged_rules_text() -> str:
    with as_file(files("local_model_router.disclosure").joinpath(CONFIG_FILENAME)) as path:
        return Path(path).read_text(encoding="utf-8")


def load_policy(path: str | Path | None = None) -> DisclosurePolicy:
    """Load rules from *path*, falling back to the packaged default.

    A missing override file is not an error — the packaged rules apply. A
    malformed override is an error, so a broken edit is never silently
    replaced by more permissive defaults.
    """
    if path is not None:
        override = Path(path)
        if override.is_file():
            try:
                data = yaml.safe_load(override.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                raise DisclosureConfigError(f"could not read {override}: {exc}") from exc
            return parse_policy(data, source=str(override))
    return parse_policy(yaml.safe_load(_packaged_rules_text()), source="packaged")

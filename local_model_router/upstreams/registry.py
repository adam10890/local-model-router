"""Upstream backend registry.

An *upstream* is an inference provider beyond the local llama.cpp fleet —
Ollama, vLLM, LocalAI, LM Studio, or any other OpenAI-compatible server.
One adapter type (``openai_compatible``) covers all of them because they
share the ``/v1`` surface; capability differences are declared per entry,
not pretended away. A second type (``subscription``) covers CLI-driven
providers with no HTTP surface at all (Codex, Ollama Cloud's CLI path) —
those carry declared usage ``limits`` instead of a ``base_url``.

Config lives in ``conf/upstreams.yaml`` next to the fleet YAML. API keys are
referenced by environment-variable name (``api_key_env``), never stored in
the file.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

TYPE_OPENAI_COMPATIBLE = "openai_compatible"
TYPE_SUBSCRIPTION = "subscription"
_KNOWN_TYPES = frozenset({TYPE_OPENAI_COMPATIBLE, TYPE_SUBSCRIPTION})

_SERVING_CAPABILITIES = ("chat", "models")

_WINDOW_RE = re.compile(r"^(\d+)([hd])$")
_WINDOW_SECONDS = {"h": 3600, "d": 86400}


def parse_window(spec: str) -> int:
    """Parse a rolling-window spec (``"5h"``, ``"7d"``) into seconds.

    Raises ``ValueError`` on anything that doesn't match ``<int><h|d>``.
    """
    match = _WINDOW_RE.match(str(spec or "").strip())
    if not match:
        raise ValueError(f"invalid window spec: {spec!r}")
    count, unit = match.groups()
    return int(count) * _WINDOW_SECONDS[unit]


@dataclass(frozen=True)
class LimitWindow:
    """A declared rolling-window usage cap (e.g. Codex's 5h/7d limits).

    Subscription providers expose no remaining-quota API, so these are
    hand-declared from the provider's published limits, not measured.
    """

    window: str
    max_tokens: Optional[int] = None
    max_requests: Optional[int] = None


@dataclass(frozen=True)
class UpstreamConfig:
    """One configured upstream provider."""

    name: str
    type: str
    base_url: str = ""
    api_key_env: str = ""
    enabled: bool = False
    experimental: bool = False
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    # Models declared eligible for auto-routing (bare ids, no "<name>/" prefix).
    # Empty means the upstream is reachable only by explicit "<name>/<model>".
    models: tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""
    # Declared rolling-window usage limits (subscription providers only have
    # these — no live quota API to poll).
    limits: tuple[LimitWindow, ...] = field(default_factory=tuple)
    # subscription-type only: how to invoke it (e.g. "codex_cli") and which
    # model id to assume when none is given.
    invoke: str = ""
    default_model: str = ""

    @property
    def serves_inference(self) -> bool:
        return self.enabled and self.type == TYPE_OPENAI_COMPATIBLE and bool(self.base_url)

    @property
    def has_declared_limits(self) -> bool:
        return bool(self.limits)

    def api_key(self, env: Optional[Dict[str, str]] = None) -> str:
        if not self.api_key_env:
            return ""
        source = os.environ if env is None else env
        return str(source.get(self.api_key_env, "") or "").strip()

    def headers(self, env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = self.api_key(env)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def describe(self) -> Dict[str, Any]:
        """Safe public description — never includes key material."""
        return {
            "name": self.name,
            "type": self.type,
            "base_url": self.base_url,
            "enabled": self.enabled,
            "experimental": self.experimental,
            "serves_inference": self.serves_inference,
            "capabilities": list(self.capabilities),
            "models": list(self.models),
            "auth_configured": bool(self.api_key_env),
            "notes": self.notes,
            "limits": [
                {"window": lw.window, "max_tokens": lw.max_tokens, "max_requests": lw.max_requests}
                for lw in self.limits
            ],
            "has_declared_limits": self.has_declared_limits,
            "invoke": self.invoke,
            "default_model": self.default_model,
        }


def _parse_limit(raw: Any) -> Optional[LimitWindow]:
    """Parse one ``limits:`` list entry. Malformed entries degrade to None
    rather than failing the whole upstream — a typo in one window shouldn't
    drop the rest of the config."""
    if not isinstance(raw, dict):
        return None
    window = str(raw.get("window") or "").strip()
    try:
        parse_window(window)
    except ValueError:
        return None

    def _optional_int(value: Any) -> Optional[int]:
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    return LimitWindow(
        window=window,
        max_tokens=_optional_int(raw.get("max_tokens")),
        max_requests=_optional_int(raw.get("max_requests")),
    )


def _parse_entry(raw: Any) -> Optional[UpstreamConfig]:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip().lower()
    type_ = str(raw.get("type") or "").strip().lower()
    if not name or type_ not in _KNOWN_TYPES:
        return None

    capabilities = raw.get("capabilities")
    if not isinstance(capabilities, list):
        capabilities = list(_SERVING_CAPABILITIES)

    models = raw.get("models")
    if not isinstance(models, list):
        models = []

    limits_raw = raw.get("limits")
    if not isinstance(limits_raw, list):
        limits_raw = []
    limits = [parsed for item in limits_raw if (parsed := _parse_limit(item)) is not None]

    experimental = bool(raw.get("experimental", False))
    enabled = bool(raw.get("enabled", False))

    return UpstreamConfig(
        name=name,
        type=type_,
        base_url=str(raw.get("base_url") or "").strip().rstrip("/"),
        api_key_env=str(raw.get("api_key_env") or "").strip(),
        enabled=enabled,
        experimental=experimental,
        capabilities=tuple(str(c) for c in capabilities),
        models=tuple(str(m).strip() for m in models if str(m).strip()),
        notes=str(raw.get("notes") or ""),
        limits=tuple(limits),
        invoke=str(raw.get("invoke") or "").strip(),
        default_model=str(raw.get("default_model") or "").strip(),
    )


def load_upstreams(path: str | Path) -> List[UpstreamConfig]:
    """Parse ``upstreams.yaml``; missing or malformed files yield []."""
    config_path = Path(path)
    if not config_path.exists():
        return []
    try:
        with open(config_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []

    entries = data.get("upstreams")
    if not isinstance(entries, list):
        return []

    out: List[UpstreamConfig] = []
    seen: set[str] = set()
    for raw in entries:
        upstream = _parse_entry(raw)
        if upstream is not None and upstream.name not in seen:
            seen.add(upstream.name)
            out.append(upstream)
    return out


def match_upstream_model(
    model: Optional[str], upstreams: List[UpstreamConfig]
) -> Optional[tuple[UpstreamConfig, str]]:
    """Match ``<upstream-name>/<model-id>`` against serving upstreams.

    Returns the upstream and the bare model id, or None when the model name
    does not target a configured upstream.
    """
    requested = str(model or "").strip()
    prefix, separator, remainder = requested.partition("/")
    if not separator or not remainder:
        return None
    prefix = prefix.lower()
    for upstream in upstreams:
        if upstream.name == prefix and upstream.serves_inference:
            return upstream, remainder
    return None

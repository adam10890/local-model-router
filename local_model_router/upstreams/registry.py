"""Upstream backend registry.

An *upstream* is an inference provider beyond the local llama.cpp fleet —
Ollama, vLLM, LocalAI, LM Studio, or any other OpenAI-compatible server.
One adapter type (``openai_compatible``) covers all of them because they
share the ``/v1`` surface; capability differences are declared per entry,
not pretended away.

Config lives in ``conf/upstreams.yaml`` next to the fleet YAML. API keys are
referenced by environment-variable name (``api_key_env``), never stored in
the file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

TYPE_OPENAI_COMPATIBLE = "openai_compatible"
_KNOWN_TYPES = frozenset({TYPE_OPENAI_COMPATIBLE})

_SERVING_CAPABILITIES = ("chat", "models")


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
    notes: str = ""

    @property
    def serves_inference(self) -> bool:
        return self.enabled and self.type == TYPE_OPENAI_COMPATIBLE and bool(self.base_url)

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
            "auth_configured": bool(self.api_key_env),
            "notes": self.notes,
        }


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
        notes=str(raw.get("notes") or ""),
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

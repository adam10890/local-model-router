"""Canonical harness configuration and atomic persistence."""
from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class HarnessConfigError(ValueError):
    """Raised when canonical harness configuration is unsafe or malformed."""


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise HarnessConfigError(f"duplicate key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


@dataclass(frozen=True)
class HarnessConnection:
    name: str
    model: str

    def describe(self) -> Dict[str, str]:
        return {"name": self.name, "model": self.model}


@dataclass(frozen=True)
class HarnessProfile:
    harness_id: str
    display_name: str
    kind: str
    protocol: str
    location: str
    connections: Dict[str, HarnessConnection]

    def describe(self) -> Dict[str, Any]:
        return {
            "harness_id": self.harness_id,
            "display_name": self.display_name,
            "kind": self.kind,
            "protocol": self.protocol,
            "location": self.location,
            "connections": [item.describe() for item in self.connections.values()],
        }


def _safe_id(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if not _ID.fullmatch(normalized):
        raise HarnessConfigError(f"invalid {label}: {normalized!r}")
    return normalized


def _parse_profile(harness_id: Any, raw: Any) -> HarnessProfile:
    normalized = _safe_id(harness_id, "harness id")
    if not isinstance(raw, dict):
        raise HarnessConfigError(f"harness {normalized!r} must be a mapping")
    connections_raw = raw.get("connections")
    if not isinstance(connections_raw, dict) or not connections_raw:
        raise HarnessConfigError(f"harness {normalized!r} requires connections")

    connections: Dict[str, HarnessConnection] = {}
    for name, connection_raw in connections_raw.items():
        connection_name = _safe_id(name, "connection name")
        if not isinstance(connection_raw, dict):
            raise HarnessConfigError(f"connection {connection_name!r} must be a mapping")
        model = str(connection_raw.get("model") or "").strip()
        if not model:
            raise HarnessConfigError(f"connection {connection_name!r} requires a model")
        connections[connection_name] = HarnessConnection(connection_name, model)

    protocol = str(raw.get("protocol") or "openai").strip().lower()
    if protocol != "openai":
        raise HarnessConfigError(f"unsupported protocol: {protocol!r}")
    location = str(raw.get("location") or "host").strip().lower()
    if location not in {"host", "docker"}:
        raise HarnessConfigError(f"unsupported location: {location!r}")
    return HarnessProfile(
        harness_id=normalized,
        display_name=str(raw.get("display_name") or normalized).strip() or normalized,
        kind=_safe_id(raw.get("kind") or normalized, "harness kind"),
        protocol=protocol,
        location=location,
        connections=connections,
    )


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader) or {}
    except HarnessConfigError:
        raise
    except (OSError, yaml.YAMLError) as exc:
        raise HarnessConfigError(f"could not read harness config: {exc}") from exc
    if not isinstance(data, dict):
        raise HarnessConfigError("harness config must be a mapping")
    return data


def _legacy_profiles(path: Optional[str | Path]) -> Dict[str, HarnessProfile]:
    if path is None or not Path(path).exists():
        return {}
    try:
        apps = (_load_yaml(Path(path)).get("apps") or {})
    except HarnessConfigError:
        return {}
    if not isinstance(apps, dict):
        return {}

    profiles: Dict[str, HarnessProfile] = {}
    for app_id, raw in apps.items():
        if app_id == "default" or not isinstance(raw, dict):
            continue
        roles = raw.get("roles") if isinstance(raw.get("roles"), dict) else {}
        connections: Dict[str, Dict[str, str]] = {}
        if str(app_id) == "agent_zero":
            for name in ("chat", "utility"):
                if str(roles.get(name) or "").strip():
                    connections[name] = {"model": str(roles[name]).strip()}
        else:
            default = str(raw.get("default_model") or "").strip()
            model = str(roles.get(default) or "").strip() if default else ""
            if not model and len(roles) == 1:
                model = str(next(iter(roles.values()))).strip()
            if model:
                connections["default"] = {"model": model}
        if not connections:
            continue
        profile = _parse_profile(app_id, {
            "display_name": raw.get("display_name") or app_id,
            "kind": app_id,
            "location": "docker" if app_id == "agent_zero" else "host",
            "connections": connections,
        })
        profiles[profile.harness_id] = profile
    return profiles


class HarnessProfiles:
    """Validated harness profiles with path resolution and atomic upsert."""

    def __init__(
        self,
        profiles: Optional[Dict[str, HarnessProfile]] = None,
        *,
        path: Optional[Path] = None,
        source: str = "canonical",
    ) -> None:
        self._profiles = dict(profiles or {})
        self.path = path
        self.source = source

    @classmethod
    def load(cls, path: str | Path, *, legacy_path: Optional[str | Path] = None) -> "HarnessProfiles":
        config_path = Path(path)
        if not config_path.exists():
            legacy = _legacy_profiles(legacy_path)
            return cls(legacy, path=config_path, source="legacy_apps" if legacy else "empty")
        data = _load_yaml(config_path)
        raw_profiles = data.get("harnesses")
        if not isinstance(raw_profiles, dict):
            raise HarnessConfigError("harness config requires a 'harnesses' mapping")
        profiles: Dict[str, HarnessProfile] = {}
        for harness_id, raw in raw_profiles.items():
            profile = _parse_profile(harness_id, raw)
            profiles[profile.harness_id] = profile
        return cls(profiles, path=config_path, source="canonical")

    def get(self, harness_id: str) -> HarnessProfile:
        return self._profiles[_safe_id(harness_id, "harness id")]

    def resolve(self, harness_id: str, connection: Optional[str] = None) -> HarnessConnection:
        profile = self.get(harness_id)
        if connection is None:
            if set(profile.connections) != {"default"}:
                raise KeyError("explicit connection required")
            connection = "default"
        return profile.connections[_safe_id(connection, "connection name")]

    def list_profiles(self) -> list[HarnessProfile]:
        return list(self._profiles.values())

    def upsert(self, payload: Dict[str, Any]) -> tuple["HarnessProfiles", Optional[Path]]:
        if self.path is None:
            raise HarnessConfigError("harness config path is unavailable")
        harness_id = payload.get("harness_id")
        raw = {key: value for key, value in payload.items() if key != "harness_id"}
        profile = _parse_profile(harness_id, raw)
        profiles = {**self._profiles, profile.harness_id: profile}
        document = {
            "harnesses": {
                item.harness_id: {
                    "display_name": item.display_name,
                    "kind": item.kind,
                    "protocol": item.protocol,
                    "location": item.location,
                    "connections": {
                        connection.name: {"model": connection.model}
                        for connection in item.connections.values()
                    },
                }
                for item in profiles.values()
            }
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        backup: Optional[Path] = None
        if self.path.exists():
            backup = self.path.with_name(f"{self.path.name}.{time.time_ns()}.bak")
            shutil.copy2(self.path, backup)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        return HarnessProfiles(profiles, path=self.path), backup

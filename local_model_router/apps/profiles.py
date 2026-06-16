"""App (client) profiles.

Each application talking to the router — Agent Zero, Hermes, n8n,
Open WebUI, custom scripts — can have a profile in ``conf/apps.yaml``:
a default model, an allowed-model policy, and notes. The app identifies
itself with the ``X-App-Id`` header (falling back to the agent identity).

Unknown apps get the permissive ``default`` profile, so adding the header
is opt-in hardening, never a breaking requirement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

DEFAULT_APP_ID = "default"


@dataclass(frozen=True)
class AppProfile:
    """Routing policy for one client application."""

    app_id: str
    default_model: str = "auto"
    allowed_models: tuple[str, ...] = ("*",)
    allow_auto_route: bool = True
    notes: str = ""

    def model_allowed(self, model: str) -> bool:
        if "*" in self.allowed_models:
            return True
        return model.strip().lower() in {m.lower() for m in self.allowed_models}

    def describe(self) -> Dict[str, Any]:
        return {
            "app_id": self.app_id,
            "default_model": self.default_model,
            "allowed_models": list(self.allowed_models),
            "allow_auto_route": self.allow_auto_route,
            "notes": self.notes,
        }


_PERMISSIVE_DEFAULT = AppProfile(app_id=DEFAULT_APP_ID)


def _parse_profile(app_id: str, raw: Any) -> Optional[AppProfile]:
    if not isinstance(raw, dict):
        return None
    allowed = raw.get("allowed_models")
    if isinstance(allowed, list) and allowed:
        allowed_tuple = tuple(str(m).strip() for m in allowed if str(m).strip())
    else:
        allowed_tuple = ("*",)
    return AppProfile(
        app_id=app_id,
        default_model=str(raw.get("default_model") or "auto").strip() or "auto",
        allowed_models=allowed_tuple or ("*",),
        allow_auto_route=bool(raw.get("allow_auto_route", True)),
        notes=str(raw.get("notes") or ""),
    )


class AppProfiles:
    """Loaded profile set with lookup + policy enforcement."""

    def __init__(self, profiles: Optional[Dict[str, AppProfile]] = None) -> None:
        self._profiles = dict(profiles or {})

    @classmethod
    def load(cls, path: str | Path) -> "AppProfiles":
        config_path = Path(path)
        if not config_path.exists():
            return cls()
        try:
            with open(config_path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except yaml.YAMLError:
            return cls()
        if not isinstance(data, dict):
            return cls()
        apps = data.get("apps")
        if not isinstance(apps, dict):
            return cls()

        profiles: Dict[str, AppProfile] = {}
        for app_id, raw in apps.items():
            normalized = str(app_id).strip().lower()
            profile = _parse_profile(normalized, raw)
            if normalized and profile is not None:
                profiles[normalized] = profile
        return cls(profiles)

    def get(self, app_id: Optional[str]) -> AppProfile:
        normalized = str(app_id or "").strip().lower()
        if normalized and normalized in self._profiles:
            return self._profiles[normalized]
        return self._profiles.get(DEFAULT_APP_ID, _PERMISSIVE_DEFAULT)

    def list_profiles(self) -> List[Dict[str, Any]]:
        return [profile.describe() for profile in self._profiles.values()]

    def apply(self, app_id: Optional[str], model: Optional[str]) -> tuple[str, Optional[str]]:
        """Resolve the effective model for an app.

        Returns ``(model, error)``. ``error`` is None when allowed; otherwise
        a short policy-violation code the caller turns into a 403.
        """
        profile = self.get(app_id)
        requested = str(model or "").strip()

        if not requested:
            requested = profile.default_model
        if requested.lower() == "auto":
            if not profile.allow_auto_route:
                return requested, "auto_route_disabled_for_app"
            return requested, None
        if not profile.model_allowed(requested):
            return requested, "model_not_allowed_for_app"
        return requested, None

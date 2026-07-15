"""
Read-only observer: loads existing fleet config and exposes status/preview data.

Never calls start_slot, stop_slot, start_all, or write to any config file.
Uses BackendManager(config_path) directly — not get_instance() — so it never
touches the plugin's singleton.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Bootstrap sys.path so local_model_router.* is importable both in the
# A0 container (/a0 root) and in the dev environment (symlink at /usr/plugins/).
_REPO_ROOT = Path(__file__).resolve().parents[2]  # → / in dev, /a0 in production
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from local_model_router.helpers.conf_resolver import resolve_conf_path
except ImportError:
    _PLUGIN_ROOT = Path(__file__).resolve().parents[1]
    if str(_PLUGIN_ROOT) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_ROOT))
    from local_model_router.helpers.conf_resolver import resolve_conf_path

# Patterns that identify sensitive config fields to redact.
_SENSITIVE_PATTERNS = (
    "api_key", "token", "secret", "password", "bearer", "auth_key",
    "private_key", "access_key",
)


def _is_sensitive(key: str) -> bool:
    lower = key.lower()
    return any(pattern in lower for pattern in _SENSITIVE_PATTERNS)


def _redact(obj: Any, depth: int = 0) -> Any:
    """Recursively redact sensitive values in a config dict."""
    if depth > 15:
        return obj
    if isinstance(obj, dict):
        return {
            k: "[REDACTED]" if _is_sensitive(k) else _redact(v, depth + 1)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(item, depth + 1) for item in obj]
    return obj


class ObserverBackend:
    """
    Read-only view of the fleet.

    Loads the existing YAML config and provides methods for each observer
    endpoint.  Never mutates runtime state.
    """

    def __init__(self, config_path: Optional[str | os.PathLike[str]] = None) -> None:
        self.config_path = os.fspath(config_path) if config_path is not None else resolve_conf_path(__file__)
        self._raw: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        self._raw = {}
        if not os.path.exists(self.config_path):
            return
        with open(self.config_path, encoding="utf-8") as f:
            self._raw = yaml.safe_load(f) or {}

    def reload(self) -> None:
        """Reload the observer after an explicit setup/configuration write."""
        self._load()

    def _make_manager(self):
        """Instantiate a fresh BackendManager from config (not the singleton)."""
        try:
            from local_model_router.helpers.llama_cpp_manager import BackendManager
        except ImportError as exc:
            raise ImportError(
                "BackendManager not found. In standalone mode, install the "
                "a0_lmm_router package path or use observer endpoints that do "
                "not require routing decisions."
            ) from exc
        return BackendManager(self.config_path)

    # ------------------------------------------------------------------
    # /slots
    # ------------------------------------------------------------------

    def get_slots(self) -> List[Dict[str, Any]]:
        """Return configured slots with safe metadata — no secrets, no paths."""
        global_cfg = self._raw.get("global", {})
        backend_type = global_cfg.get("backend", "auto")
        lmm_hosts: Dict[str, str] = global_cfg.get("lmm_hosts", {})
        slots = []

        for slot in self._raw.get("active_slots", []):
            if not slot:
                continue
            slot_id = slot.get("id") or f"slot_{slot.get('port', '?')}"
            port = slot.get("port")
            host = slot.get("host", "localhost")
            role = slot.get("role", "")
            enabled = slot.get("enabled", True)

            # Prefer lmm_hosts URL for remote backend when role matches
            if backend_type == "remote" and role and role in lmm_hosts:
                remote = lmm_hosts[role]
                base_url = f"http://{remote}/v1"
            elif port:
                base_url = f"http://{host}:{port}/v1"
            else:
                base_url = None

            slots.append({
                "id": slot_id,
                "host": host,
                "port": port,
                "role": role,
                "enabled": enabled,
                "backend_type": backend_type,
                "base_url": base_url,
                "model_id": slot.get("model_id"),
                "router_mode": slot.get("router_mode", False),
                "context_size": slot.get("context_size"),
                "supports_tools": slot.get("supports_tools"),
                "supports_vision": slot.get("supports_vision"),
                "supports_json_mode": slot.get("supports_json_mode"),
                "latency_hint_ms": slot.get("latency_hint_ms"),
                "quality_hint": slot.get("quality_hint"),
                "resource_cost_hint": slot.get("resource_cost_hint"),
                "parallel_slots": slot.get("parallel_slots"),
                "router_models_preset": slot.get("router_models_preset", ""),
                "router_models_max": slot.get("router_models_max"),
            })

        return slots

    # ------------------------------------------------------------------
    # /config/preview
    # ------------------------------------------------------------------

    def get_config_preview(self) -> Dict[str, Any]:
        """Return sanitized config — secrets redacted, structure intact."""
        return _redact(self._raw)

    # ------------------------------------------------------------------
    # /routing/preview
    # ------------------------------------------------------------------

    async def get_routing_preview(self, role: str) -> Dict[str, Any]:
        """Return which slot would be selected for *role* using current routing logic."""
        try:
            mgr = self._make_manager()
        except Exception as exc:
            return {
                "role": role,
                "slot_id": None,
                "url": None,
                "is_failover": False,
                "chain": [],
                "no_slot_available": True,
                "error": f"{type(exc).__name__}: {exc}",
            }

        decision = await mgr.select_slot_with_failover_async(role)
        if decision:
            return {
                "role": role,
                "slot_id": decision.get("slot_id"),
                "url": decision.get("url"),
                "is_failover": decision.get("is_failover", False),
                "chain": decision.get("fallback_chain", []),
                "no_slot_available": False,
            }
        return {
            "role": role,
            "slot_id": None,
            "url": None,
            "is_failover": False,
            "chain": [],
            "no_slot_available": True,
        }

    # ------------------------------------------------------------------
    # /health/slots
    # ------------------------------------------------------------------

    async def get_slots_health(self) -> List[Dict[str, Any]]:
        """Probe each slot's /health endpoint and return status."""
        from local_model_router.helpers.smart_router.health import SlotHealthChecker
        checker = SlotHealthChecker()
        results = []

        for slot in self.get_slots():
            if not slot.get("enabled"):
                results.append({**slot, "health": "disabled"})
                continue
            try:
                config = {"host": slot["host"], "port": slot["port"]}
                health = await checker.check_async(config)
            except Exception:
                health = "unknown"
            results.append({**slot, "health": health})

        return results

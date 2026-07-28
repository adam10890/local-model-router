"""Opt-in fleet lifecycle control: start/stop llama.cpp slots over HTTP.

By default the router is lifecycle-free — it routes to slots that are
already running and never starts or stops anything. Setting
``A0_LMM_ROUTER_ENABLE_FLEET_CONTROL=1`` exposes the ``/fleet/...``
start/stop endpoints, which delegate to ``BackendManager`` — the same
orchestration layer the MCP admin tools use.

What "start" means is decided by ``global.backend`` in the fleet config:

- ``docker``     — start/stop real llama.cpp containers with the full
                   flag set rendered from the slot config (requires the
                   ``[docker]`` extra and a reachable Docker daemon).
- ``subprocess`` — spawn/kill local llama-server processes.
- ``remote``     — register and health-verify pre-running servers only;
                   no process control.

Security: these endpoints honor the bearer API key like every other
endpoint, and additionally refuse with ``fleet_control_disabled`` unless
the env flag is set. The flag is read at app creation time.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional

ENABLE_ENV = "A0_LMM_ROUTER_ENABLE_FLEET_CONTROL"

_TRUTHY = {"1", "true", "yes", "on"}


def fleet_control_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the start/stop surface should be exposed."""
    source: Mapping[str, str] = env if env is not None else os.environ
    return str(source.get(ENABLE_ENV, "")).strip().lower() in _TRUTHY


def configured_backend(config_path: Optional[str]) -> str:
    """Read ``global.backend`` from the fleet YAML without building a backend."""
    if not config_path or not os.path.exists(config_path):
        return "unknown"
    try:
        import yaml

        with open(config_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return str((data.get("global") or {}).get("backend", "auto")).lower()
    except Exception:
        return "unknown"


class FleetControlError(Exception):
    """Structured failure for the HTTP layer to map onto an error response."""

    def __init__(self, message: str, code: str, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class FleetControlHandler:
    """Thin async facade over ``BackendManager`` lifecycle operations."""

    def __init__(self, config_path: Optional[str] = None) -> None:
        self._config_path = config_path

    def _manager(self):
        from local_model_router.helpers.llama_cpp_manager import BackendManager

        return BackendManager.get_instance(self._config_path)

    @staticmethod
    def _raise_for_error(result: Any, slot_id: str) -> None:
        error = result.get("error") if isinstance(result, dict) else None
        if not error:
            return
        text = str(error)
        if "not found in config" in text:
            raise FleetControlError(text, "unknown_slot", 404)
        if "No backend initialized" in text:
            raise FleetControlError(text, "backend_unavailable", 503)

    async def start_slot(self, slot_id: str) -> Dict[str, Any]:
        manager = self._manager()
        try:
            result = await manager.start_slot(slot_id)
        except ImportError as exc:
            raise FleetControlError(
                f"backend dependency missing: {exc}. "
                'Install it with: pip install "local-model-router[docker]"',
                "backend_dependency_missing",
                503,
            )
        self._raise_for_error(result, slot_id)
        return {
            "ok": bool(isinstance(result, dict) and result.get("running") and not result.get("error")),
            "action": "start",
            "slot": slot_id,
            "backend": manager.backend_type,
            "result": result,
        }

    async def stop_slot(self, slot_id: str) -> Dict[str, Any]:
        manager = self._manager()
        if slot_id not in manager._slot_configs:
            raise FleetControlError(f"Slot '{slot_id}' not found in config", "unknown_slot", 404)
        stopped = await manager.stop_slot(slot_id)
        return {
            "ok": bool(stopped),
            "action": "stop",
            "slot": slot_id,
            "backend": manager.backend_type,
        }

    async def status(self) -> Dict[str, Dict[str, Any]]:
        from local_model_router.helpers.llama_cpp_manager import BackendManager

        return await BackendManager._instance.status() if BackendManager._instance else {}

    async def start_all(self) -> Dict[str, Any]:
        manager = self._manager()
        try:
            results = await manager.start_all()
        except ImportError as exc:
            raise FleetControlError(
                f"backend dependency missing: {exc}. "
                'Install it with: pip install "local-model-router[docker]"',
                "backend_dependency_missing",
                503,
            )
        return {
            "ok": all(not (r or {}).get("error") for r in results.values()) if results else False,
            "action": "start_all",
            "backend": manager.backend_type,
            "results": results,
        }

    async def stop_all(self) -> Dict[str, Any]:
        manager = self._manager()
        await manager.stop_all()
        return {"ok": True, "action": "stop_all", "backend": manager.backend_type}

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

    def __init__(self, config_path: Optional[str] = None, *, manager: Any = None) -> None:
        self._config_path = config_path
        self._injected_manager = manager

    def _manager(self):
        if self._injected_manager is not None:
            return self._injected_manager
        from local_model_router.helpers.llama_cpp_manager import BackendManager

        return BackendManager.get_instance(self._config_path)

    def _manager_or_dependency_error(self):
        try:
            return self._manager()
        except ImportError as exc:
            raise FleetControlError(
                f"backend dependency missing: {exc}. "
                'Install it with: pip install "local-model-router[docker]"',
                "backend_dependency_missing",
                503,
            ) from exc

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
        if result.get("failure_code") == "slot_operation_in_progress":
            raise FleetControlError(text, "slot_operation_in_progress", 409)
        code = str(result.get("failure_code") or "slot_start_failed")
        status = 409 if code in {
            "port_in_use",
            "process_identity_changed",
            "process_ownership_unverified",
        } else 503
        raise FleetControlError(
            f"Slot '{slot_id}' could not be started",
            code,
            status,
        )

    async def start_slot(self, slot_id: str) -> Dict[str, Any]:
        manager = self._manager_or_dependency_error()
        result = await manager.start_slot(slot_id)
        self._raise_for_error(result, slot_id)
        return {
            "ok": bool(isinstance(result, dict) and result.get("running") and not result.get("error")),
            "action": "start",
            "slot": slot_id,
            "backend": manager.backend_type,
            "result": result,
        }

    async def stop_slot(self, slot_id: str) -> Dict[str, Any]:
        manager = self._manager_or_dependency_error()
        if slot_id not in manager._slot_configs:
            raise FleetControlError(f"Slot '{slot_id}' not found in config", "unknown_slot", 404)
        stopped = await manager.stop_slot(slot_id)
        if not stopped and manager.slot_operation_snapshot(slot_id)["phase"] != "idle":
            raise FleetControlError(
                f"Slot '{slot_id}' already has an operation in progress",
                "slot_operation_in_progress",
                409,
            )
        if not stopped:
            failure_code = "slot_stop_failed"
            candidate = str(
                getattr(manager, "last_slot_failure_code", lambda _name: "")(
                    slot_id
                )
                or ""
            )
            if candidate in {
                "drain_timeout",
                "process_identity_changed",
                "process_ownership_unverified",
            }:
                failure_code = candidate
            raise FleetControlError(
                f"Slot '{slot_id}' could not be stopped safely",
                failure_code,
                409,
            )
        return {
            "ok": True,
            "action": "stop",
            "slot": slot_id,
            "backend": manager.backend_type,
        }

    async def status(self) -> Dict[str, Dict[str, Any]]:
        if self._injected_manager is not None:
            return await self._injected_manager.status()
        from local_model_router.helpers.llama_cpp_manager import BackendManager

        return await BackendManager._instance.status() if BackendManager._instance else {}

    async def start_all(self) -> Dict[str, Any]:
        manager = self._manager_or_dependency_error()
        results = await manager.start_all()
        failures = {
            slot_id: str(result.get("failure_code") or "slot_start_failed")
            for slot_id, result in results.items()
            if isinstance(result, dict) and result.get("error")
        }
        return {
            "ok": bool(results) and not failures,
            "action": "start_all",
            "backend": manager.backend_type,
            "results": results,
            "failures": failures,
        }

    async def stop_all(self) -> Dict[str, Any]:
        manager = self._manager_or_dependency_error()
        results = await manager.stop_all()
        failed = [name for name, stopped in results.items() if not stopped]
        failures = {
            name: (
                "slot_operation_in_progress"
                if manager.slot_operation_snapshot(name)["phase"] != "idle"
                else str(
                    getattr(manager, "last_slot_failure_code", lambda _name: "")(name)
                    or "fleet_stop_incomplete"
                )
            )
            for name in failed
        }
        return {
            "ok": not failures,
            "action": "stop_all",
            "backend": manager.backend_type,
            "results": results,
            "failures": failures,
        }

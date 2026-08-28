"""Shared, sanitized diagnostics for the CLI and support report."""
from __future__ import annotations

import importlib
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

PROBE_TIMEOUT_SECONDS = 3.0

DEPENDENCIES = {
    "aiohttp": ("aiohttp", "ClientSession"),
    "pydantic": ("pydantic", "BaseModel"),
    "starlette": ("starlette.applications", "Starlette"),
    "uvicorn": ("uvicorn", "run"),
    "yaml": ("yaml", "safe_load"),
}

_SAFE_CODE = re.compile(r"[^a-z0-9_.-]+")


def probe_url(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT_SECONDS) as response:  # noqa: S310
            return 200 <= response.status < 500
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def _code(label: str) -> str:
    """Preserve the existing doctor code contract."""
    return label.lower().replace(" ", "_").replace(":", "")


def _safe_identifier(value: Any, fallback: str) -> str:
    candidate = _SAFE_CODE.sub("_", str(value or "").strip().lower()).strip("_.-")
    return candidate[:80] or fallback


def _check(
    label: str,
    ok: bool,
    detail: str = "",
    remediation: str = "",
) -> dict[str, Any]:
    return {
        "code": _code(label),
        "status": "pass" if ok else "fail",
        "severity": "info" if ok else "blocking",
        "label": label,
        "detail": detail,
        "remediation": remediation or None,
    }


def collect_doctor_checks(
    config_path: str,
    *,
    probe: Callable[[str], bool] = probe_url,
    import_module: Callable[[str], Any] = importlib.import_module,
    include_locations: bool = True,
) -> dict[str, Any]:
    """Run the canonical doctor checks without mutating configuration or runtime."""
    checks: list[dict[str, Any]] = []
    checks.append(
        _check(
            "python >= 3.10",
            sys.version_info >= (3, 10),
            f"running {sys.version_info.major}.{sys.version_info.minor}",
        )
    )

    for name, (module_name, symbol) in DEPENDENCIES.items():
        capability = f"{module_name}.{symbol}"
        try:
            module = import_module(module_name)
            if not callable(getattr(module, symbol, None)):
                raise AttributeError(symbol)
            checks.append(_check(f"dependency: {name}", True, capability))
        except (ImportError, AttributeError):
            checks.append(
                _check(
                    f"dependency: {name}",
                    False,
                    f"required capability unavailable: {capability}",
                    f"Reinstall {name} in the Imperium Python environment",
                )
            )

    config_ok = os.path.exists(config_path)
    config_detail = config_path if include_locations else ("configured" if config_ok else "not found")
    checks.append(_check("config file exists", config_ok, config_detail))

    slots: list[dict[str, Any]] = []
    if config_ok:
        try:
            from local_model_router.service.observer import ObserverBackend

            slots = ObserverBackend(config_path).get_slots()
            checks.append(_check("config parses", True, f"{len(slots)} slot(s)"))
        except Exception:
            checks.append(_check("config parses", False, "configuration could not be parsed"))

    reachable = 0
    enabled = 0
    for index, slot in enumerate(slots, start=1):
        if not slot.get("enabled") or not slot.get("base_url"):
            continue
        enabled += 1
        slot_id = _safe_identifier(slot.get("id"), f"slot_{index}")
        url = str(slot["base_url"]).rstrip("/") + "/models"
        ok = probe(url)
        reachable += int(ok)
        detail = url if include_locations else ("slot responded" if ok else "slot did not respond")
        checks.append(
            _check(
                f"slot reachable: {slot_id}",
                ok,
                detail,
                "Start the configured model server",
            )
        )

    return {
        "ok": all(check["status"] == "pass" for check in checks),
        "checks": checks,
        "summary": {"enabled_slots": enabled, "reachable_slots": reachable},
    }


def _translated(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {"en": "", "he": ""}
    return {"en": str(value.get("en") or "")[:500], "he": str(value.get("he") or "")[:500]}


def _action(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    href = str(value.get("href") or "")
    return {
        "code": _safe_identifier(value.get("code"), "unknown_action"),
        "href": href if href.startswith("#/") else "",
        "label": _translated(value.get("label")),
    }


def _issues(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    rows = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        rows.append(
            {
                "code": _safe_identifier(value.get("code"), "unknown_issue"),
                "category": _safe_identifier(value.get("category"), "system"),
                "severity": _safe_identifier(value.get("severity"), "unknown"),
                "message": _translated(value.get("message")),
                "action": _action(value.get("action")),
            }
        )
    return rows


def sanitized_readiness(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "overall": _safe_identifier(value.get("overall"), "unknown"),
        "blocking_issues": _issues(value.get("blocking_issues")),
        "optional_issues": _issues(value.get("optional_issues")),
        "next_action": _action(value.get("next_action")),
    }


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def sanitized_hardware(value: Mapping[str, Any]) -> dict[str, Any]:
    cpu = value.get("cpu") if isinstance(value.get("cpu"), Mapping) else {}
    ram = value.get("ram") if isinstance(value.get("ram"), Mapping) else {}
    gpus = value.get("gpus") if isinstance(value.get("gpus"), Sequence) else []
    return {
        "available": value.get("available") is True,
        "cpu": {"utilization_pct": _number(cpu.get("utilization_pct"))},
        "ram": {
            key: _number(ram.get(key))
            for key in ("total_mb", "used_mb", "available_mb", "utilization_pct")
        },
        "gpus": [
            {
                key: _number(gpu.get(key))
                for key in (
                    "id",
                    "total_vram_mb",
                    "used_vram_mb",
                    "free_vram_mb",
                    "utilization_pct",
                    "temperature_c",
                )
            }
            for gpu in gpus
            if isinstance(gpu, Mapping)
        ],
    }


def sanitized_slots(
    values: Sequence[Mapping[str, Any]],
    managed: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    managed = managed or {}
    rows = []
    for index, value in enumerate(values, start=1):
        slot_id = _safe_identifier(value.get("id"), f"slot_{index}")
        health = _safe_identifier(value.get("health"), "unknown")
        runtime = managed.get(str(value.get("id") or "")) or value.get("runtime") or {}
        runtime = runtime if isinstance(runtime, Mapping) else {}
        failure_code = _safe_identifier(runtime.get("failure_code"), "") or None
        if not failure_code and health in {"unhealthy", "unknown"}:
            failure_code = "health_probe_failed" if health == "unhealthy" else "health_unknown"
        rows.append(
            {
                "id": slot_id,
                "role": _safe_identifier(value.get("role"), "unknown"),
                "enabled": value.get("enabled") is not False,
                "backend": _safe_identifier(value.get("backend_type"), "unknown"),
                "health": health,
                "runtime": {
                    "running": runtime.get("running") if isinstance(runtime.get("running"), bool) else None,
                    "healthy": runtime.get("healthy") if isinstance(runtime.get("healthy"), bool) else None,
                    "failure_code": failure_code,
                    "exit_code": _number(runtime.get("exit_code")),
                    "restart_count": _number(runtime.get("restart_count")),
                    "uptime_s": _number(runtime.get("uptime_s")),
                },
            }
        )
    return rows


def build_diagnostics_report(
    *,
    generated_at: str,
    imperium_version: str,
    doctor: Mapping[str, Any],
    readiness: Mapping[str, Any],
    slots: Sequence[Mapping[str, Any]],
    hardware: Mapping[str, Any],
    backend: str,
    fleet_control_enabled: bool,
    fleet_control_supported: bool,
    auth_enabled: bool,
    managed_slots: Mapping[str, Mapping[str, Any]] | None = None,
    collection_errors: Sequence[str] = (),
) -> dict[str, Any]:
    """Build the stable report exclusively from allowlisted operational fields."""
    doctor_payload = {
        "ok": doctor.get("ok") is True,
        "checks": [
            {
                key: check.get(key)
                for key in ("code", "status", "severity", "label", "detail", "remediation")
            }
            for check in doctor.get("checks", [])
            if isinstance(check, Mapping)
        ],
    }
    readiness_payload = sanitized_readiness(readiness)
    slot_rows = sanitized_slots(slots, managed_slots)
    errors = [
        {"component": code.split("_", 1)[0], "code": _safe_identifier(code, "collection_failed")}
        for code in collection_errors
    ]
    ok = (
        doctor_payload["ok"]
        and readiness_payload["overall"] == "ready"
        and not errors
    )
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "imperium_version": imperium_version,
        "ok": ok,
        "readiness": readiness_payload,
        "doctor": doctor_payload,
        "slots": slot_rows,
        "hardware": sanitized_hardware(hardware),
        "runtime": {
            "backend": _safe_identifier(backend, "unknown"),
            "active_slots": sum(row["enabled"] is True for row in slot_rows),
            "fleet_control_enabled": bool(fleet_control_enabled),
            "fleet_control_supported": bool(fleet_control_supported),
            "auth_enabled": bool(auth_enabled),
        },
        "collection_errors": errors,
    }

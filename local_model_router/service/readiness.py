"""Stable, translated readiness data for the Simple dashboard."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from local_model_router.routing.aliases import ALIAS_TO_ROLE, AUTO

_GENERIC_MODEL_IDS = frozenset({"", "local", AUTO, "default"}) | frozenset(ALIAS_TO_ROLE)
_HEALTHY_VALUES = {"healthy", "ok", "ready", "running"}


def _text(en: str, he: str) -> dict[str, str]:
    return {"en": en, "he": he}


def _action(code: str, href: str, en: str, he: str) -> dict[str, Any]:
    return {"code": code, "href": href, "label": _text(en, he)}


def _issue(
    code: str,
    category: str,
    severity: str,
    href: str,
    en: str,
    he: str,
    *,
    action_en: str = "Resolve",
    action_he: str = "פתרון",
) -> dict[str, Any]:
    return {
        "code": code,
        "category": category,
        "severity": severity,
        "message": _text(en, he),
        "action": _action(f"resolve_{code}", href, action_en, action_he),
    }


def display_model_stem(value: str) -> str:
    """Filename stem for a GGUF path or model id; never a directory path."""
    text = str(value or "").strip().strip('"')
    if not text:
        return ""
    name = Path(text.replace("\\", "/")).name
    if name.lower().endswith(".gguf"):
        return name[:-5]
    return name


def is_generic_model_id(value: str) -> bool:
    """Hermes/router aliases such as ``local`` are not a running GGUF identity."""
    raw = str(value or "").strip().strip('"')
    if not raw:
        return True
    normalized = raw.replace("\\", "/")
    if "/" in normalized or raw.lower().endswith(".gguf"):
        return False
    return raw.lower() in _GENERIC_MODEL_IDS


def model_from_llama_cmdline(argv: Iterable[str] | None) -> tuple[int | None, str]:
    """Read ``--port`` and ``-m`` / ``--model`` from a llama-server command line."""
    args = [str(part) for part in (argv or [])]
    port: int | None = None
    model = ""
    index = 0
    while index < len(args):
        key = args[index]
        nxt = args[index + 1] if index + 1 < len(args) else ""
        if key in {"-m", "--model"} and nxt and not nxt.startswith("-"):
            model = nxt
            index += 2
            continue
        if key.startswith("--model="):
            model = key.split("=", 1)[1]
            index += 1
            continue
        if key == "--port" and nxt.isdigit():
            port = int(nxt)
            index += 2
            continue
        if key.startswith("--port=") and key.split("=", 1)[1].isdigit():
            port = int(key.split("=", 1)[1])
            index += 1
            continue
        index += 1
    return port, display_model_stem(model)


def inspect_running_llama_models(iter_processes: Iterable[Any] | None = None) -> dict[int, str]:
    """Map llama-server listen ports to the GGUF stem from the live process argv."""
    found: dict[int, str] = {}
    iterator = iter_processes
    if iterator is None:
        try:
            import psutil
        except ImportError:
            return {}
        try:
            iterator = psutil.process_iter(["name", "cmdline"])
        except (psutil.Error, OSError):
            return {}
    for proc in iterator:
        try:
            info = proc if isinstance(proc, dict) else getattr(proc, "info", None)
            if not isinstance(info, dict):
                continue
            name = str(info.get("name") or "").lower()
            cmdline = info.get("cmdline") or []
            if not isinstance(cmdline, (list, tuple)):
                continue
            joined = " ".join(str(part) for part in cmdline).lower()
            if "llama-server" not in name and "llama-server" not in joined:
                continue
            port, model = model_from_llama_cmdline(cmdline)
            if port and model and not is_generic_model_id(model):
                found[port] = model
        except (TypeError, ValueError, OSError):
            continue
    return found


def active_slot_from_health(slots_health: list[dict[str, Any]]) -> dict[str, Any]:
    healthy_slots = [
        slot for slot in slots_health if str(slot.get("health") or "").lower() in _HEALTHY_VALUES
    ]
    return healthy_slots[0] if healthy_slots else next(
        (slot for slot in slots_health if slot.get("enabled")),
        {},
    )


def choose_running_model(
    slot: dict[str, Any],
    *,
    process_model: str = "",
    live_ids: list[str] | None = None,
) -> tuple[str, str]:
    """Prefer the live process file, then slot ``/v1/models``, never ``local``."""
    for candidate, source in (
        (process_model, "process"),
        *((item, "live_slot") for item in live_ids or []),
        (slot.get("router_default_model"), "slot"),
        (slot.get("model_id"), "slot"),
    ):
        if is_generic_model_id(str(candidate or "")):
            continue
        name = display_model_stem(str(candidate))
        if name:
            return name, source
    return "", ""


def build_ui_status(
    *,
    setup_state: dict[str, Any],
    slots_health: list[dict[str, Any]],
    compute: dict[str, Any],
    base_url: str,
    process_models: dict[int, str] | None = None,
    live_ids: list[str] | None = None,
) -> dict[str, Any]:
    discovery = setup_state.get("discovery") or {}
    hardware = setup_state.get("hardware") or {}
    platform_support = setup_state.get("platform_support") or {}
    runtime_ready = bool(discovery.get("runtime_installed")) or bool(
        discovery.get("servers") and discovery.get("enabled_slots")
    )
    model_ready = bool(discovery.get("gguf_models")) or bool(
        discovery.get("servers") and discovery.get("enabled_slots")
    )
    config_ready = bool(discovery.get("config_exists") and discovery.get("enabled_slots"))
    healthy_slots = [
        slot for slot in slots_health if str(slot.get("health") or "").lower() in _HEALTHY_VALUES
    ]
    server_ready = bool(healthy_slots)
    fleet_backend = next(
        (
            str(slot.get("backend_type") or "").lower()
            for slot in slots_health
            if slot.get("enabled")
        ),
        "unknown",
    )
    externally_managed = fleet_backend == "remote"

    recommendation = setup_state.get("recommendation") or {}
    live_ram = compute.get("ram") or hardware.get("ram") or {}
    available_mb = live_ram.get("available_mb")
    memory_required_gb = (
        float(recommendation.get("size_gb") or 0)
        + float(recommendation.get("estimated_kv_cache_gb") or 0)
        + 1.0
    )
    memory_pressure = (
        not server_ready
        and bool(recommendation)
        and isinstance(available_mb, (int, float))
        and not isinstance(available_mb, bool)
        and float(available_mb) / 1024 < memory_required_gb
    )

    blocking: list[dict[str, Any]] = []
    optional: list[dict[str, Any]] = []
    if memory_pressure:
        blocking.append(
            _issue(
                "memory_pressure",
                "system",
                "blocking",
                "#/setup/hardware",
                "Close other model servers or applications, then scan the system again.",
                "יש לסגור שרתי מודלים או יישומים אחרים ואז לסרוק שוב את המערכת.",
            )
        )
    if platform_support.get("status") not in {None, "supported"} and not discovery.get("existing_server_available"):
        blocking.append(
            _issue(
                "platform_planned",
                "configuration",
                "blocking",
                "#/setup/runtime",
                "Managed installation is planned for this platform; connect an existing local server.",
                "התקנה מנוהלת מתוכננת לפלטפורמה זו; יש לחבר שרת מקומי קיים.",
            )
        )
    if not runtime_ready and platform_support.get("status") in {None, "supported"}:
        blocking.append(
            _issue(
                "runtime_missing",
                "configuration",
                "blocking",
                "#/setup/runtime",
                "llama.cpp is not installed or connected.",
                "llama.cpp אינו מותקן או מחובר.",
            )
        )
    if not model_ready:
        blocking.append(
            _issue(
                "model_missing",
                "configuration",
                "blocking",
                "#/models/recommended",
                "Choose a local model to continue.",
                "כדי להמשיך יש לבחור מודל מקומי.",
            )
        )
    if runtime_ready and model_ready and not config_ready:
        blocking.append(
            _issue(
                "configuration_missing",
                "configuration",
                "blocking",
                "#/setup/plan",
                "Finish the local server configuration.",
                "יש להשלים את הגדרת השרת המקומי.",
            )
        )
    if config_ready and not server_ready:
        blocking.append(
            _issue(
                "server_stopped",
                "system",
                "blocking",
                "#/advanced/fleet",
                (
                    "The external model server is not responding. "
                    "Start it outside Imperium, then refresh."
                    if externally_managed
                    else "The model server is configured but not responding."
                ),
                (
                    "שרת המודל החיצוני אינו מגיב. יש להפעיל אותו מחוץ ל־Imperium ואז לרענן."
                    if externally_managed
                    else "שרת המודל מוגדר אך אינו מגיב."
                ),
                action_en="View guidance" if externally_managed else "Open fleet controls",
                action_he="הצגת הנחיות" if externally_managed else "פתיחת בקרות Fleet",
            )
        )

    disk = hardware.get("disk") or {}
    free_gb = disk.get("free_gb")
    if isinstance(free_gb, (int, float)) and free_gb < 10:
        optional.append(
            _issue(
                "low_disk_space",
                "system",
                "optional",
                "#/advanced/diagnostics",
                "Less than 10 GB is free in the model destination.",
                "בתיקיית המודלים נותרו פחות מ־10GB פנויים.",
            )
        )

    if server_ready:
        overall = "ready"
        next_action = _action("start_chat", "#/chat", "Start chatting", "התחלת שיחה")
    elif memory_pressure:
        overall = "needs_attention"
        next_action = _action("free_memory", "#/setup/hardware", "Close apps and scan again", "סגירת יישומים וסריקה מחדש")
    elif platform_support.get("status") not in {None, "supported"}:
        overall = "setup_required"
        next_action = _action("connect_existing", "#/setup/runtime", "Connect an existing server", "חיבור שרת קיים")
    elif not runtime_ready:
        overall = "setup_required"
        next_action = _action("install_runtime", "#/setup/runtime", "Install llama.cpp", "התקנת llama.cpp")
    elif not model_ready:
        overall = "setup_required"
        next_action = _action("choose_model", "#/models/recommended", "Choose a model", "בחירת מודל")
    elif not config_ready:
        overall = "setup_required"
        next_action = _action("finish_setup", "#/setup/plan", "Finish setup", "סיום ההגדרה")
    else:
        overall = "needs_attention"
        next_action = _action(
            "open_fleet",
            "#/advanced/fleet",
            "Open server guidance" if externally_managed else "Open fleet controls",
            "פתיחת הנחיות השרת" if externally_managed else "פתיחת בקרות הצי",
        )

    active_slot = active_slot_from_health(slots_health)
    try:
        active_port = int(active_slot.get("port") or 0)
    except (TypeError, ValueError):
        active_port = 0
    running_name, running_source = choose_running_model(
        active_slot,
        process_model=(process_models or {}).get(active_port, ""),
        live_ids=live_ids,
    )
    gpus = hardware.get("gpus") or compute.get("gpus") or []
    gpu = gpus[0] if gpus else {}
    ram = live_ram
    hardware_summary = {
        "gpu": gpu.get("name") or None,
        "vram_mb": gpu.get("dedicated_vram_mb") or gpu.get("total_vram_mb"),
        "ram_mb": ram.get("total_mb"),
        "ram_available_mb": ram.get("available_mb"),
        "backend": setup_state.get("recommended_backend") or active_slot.get("backend_type") or "unknown",
    }
    return {
        "schema_version": 1,
        "overall": overall,
        "active": {
            "model": running_name or None,
            "source": running_source or None,
            "backend": active_slot.get("backend_type") or hardware_summary["backend"],
            "slot_id": active_slot.get("id"),
        },
        "hardware": hardware_summary,
        "api": {"base_url": base_url, "models_url": f"{base_url}/v1/models"},
        "blocking_issues": blocking,
        "optional_issues": optional,
        "next_action": next_action,
        "quick_actions": [
            _action("open_chat", "#/chat", "Chat", "צ׳אט"),
            _action("connect_app", "#/connections", "Connect an app", "חיבור אפליקציה"),
            _action("manage_models", "#/models", "Manage models", "ניהול מודלים"),
        ],
        "stages": [
            {"code": "system", "complete": True, "label": _text("System checked", "המערכת נבדקה")},
            {"code": "runtime", "complete": runtime_ready, "label": _text("llama.cpp", "llama.cpp")},
            {"code": "model", "complete": model_ready, "label": _text("Model", "מודל")},
            {"code": "server", "complete": server_ready, "label": _text("Ready", "מוכן")},
        ],
    }

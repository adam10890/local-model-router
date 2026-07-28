"""Stable, translated readiness data for the Simple dashboard."""
from __future__ import annotations

from typing import Any


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
) -> dict[str, Any]:
    return {
        "code": code,
        "category": category,
        "severity": severity,
        "message": _text(en, he),
        "action": _action(f"resolve_{code}", href, "Resolve", "פתרון"),
    }


def build_ui_status(
    *,
    setup_state: dict[str, Any],
    slots_health: list[dict[str, Any]],
    compute: dict[str, Any],
    base_url: str,
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
    healthy_values = {"healthy", "ok", "ready", "running"}
    healthy_slots = [
        slot for slot in slots_health if str(slot.get("health") or "").lower() in healthy_values
    ]
    server_ready = bool(healthy_slots)

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
                "The model server is configured but not responding.",
                "שרת המודל מוגדר אך אינו מגיב.",
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
        next_action = _action("open_fleet", "#/advanced/fleet", "Open fleet controls", "פתיחת בקרות הצי")

    active_slot = healthy_slots[0] if healthy_slots else next(
        (slot for slot in slots_health if slot.get("enabled")),
        {},
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
            "model": active_slot.get("model_id"),
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

"""Persistent failover counters used by BackendManager status."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional

logger = logging.getLogger("local_model_router.stats")

_lock = Lock()
_stats: Optional[Dict[str, Any]] = None
_stats_path: Optional[Path] = None


def _default_stats() -> Dict[str, Any]:
    return {
        "version": 2,
        "failovers": {
            "total": 0,
            "by_reason": {},
            "by_slot": {},
            "last_at": None,
        },
    }


def _get_stats_path(plugin_dir: Optional[str | Path] = None) -> Path:
    global _stats_path
    if _stats_path is None:
        root = Path(plugin_dir) / "data" if plugin_dir else Path(__file__).resolve().parent.parent / "data"
        _stats_path = root / "stats.json"
    return _stats_path


def _normalized_stats(data: Dict[str, Any]) -> Dict[str, Any]:
    old = data.get("failovers") if isinstance(data, dict) else None
    old = old if isinstance(old, dict) else {}
    return {
        "version": 2,
        "failovers": {
            "total": int(old.get("total", old.get("total_failovers", 0)) or 0),
            "by_reason": dict(old.get("by_reason") or {}),
            "by_slot": dict(old.get("by_slot") or {}),
            "last_at": old.get("last_at", old.get("last_failover_at")),
        },
    }


def load_stats(plugin_dir: Optional[str | Path] = None) -> Dict[str, Any]:
    """Load failover counters, accepting the legacy stats schema."""
    global _stats
    with _lock:
        if _stats is not None:
            return _stats
        path = _get_stats_path(plugin_dir)
        try:
            _stats = _normalized_stats(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            _stats = _default_stats()
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Failed to load failover stats: %s", exc)
            _stats = _default_stats()
        return _stats


def save_stats() -> None:
    """Persist the compact failover counters."""
    with _lock:
        if _stats is None:
            return
        path = _get_stats_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(_stats, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to save failover stats: %s", exc)


def record_failover(from_slot: str, to_slot: str, reason: str) -> None:
    """Record one failover while retaining the established call signature."""
    del to_slot
    stats = load_stats()
    with _lock:
        failovers = stats["failovers"]
        failovers["total"] += 1
        failovers["last_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        failovers["by_reason"][reason] = failovers["by_reason"].get(reason, 0) + 1
        failovers["by_slot"][from_slot] = failovers["by_slot"].get(from_slot, 0) + 1
    save_stats()


def get_stats_summary(window: str = "24h") -> Dict[str, Any]:
    """Return the compact shape consumed by BackendManager."""
    failovers = load_stats()["failovers"]
    return {
        "window": window,
        "failovers": {
            "total": failovers["total"],
            "by_reason": dict(failovers["by_reason"]),
            "by_slot": dict(failovers["by_slot"]),
            "last_at": failovers["last_at"],
        },
        "slots": [],
    }

"""
Stats Tracker for LMM Router.

Tracks slot usage, request counts, token consumption, and estimated savings.
Adapted from tiny_router/tiny_router_helpers/stats.py.

Key features:
- Per-slot request counts and timing
- Token tracking (input/output)
- Savings estimation (local vs API costs)
- 24h/7d/30d window aggregation
- Persistent JSON storage
"""

from __future__ import annotations

import json
import time
import logging
from pathlib import Path
from threading import Lock
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("lmm_router.stats")

_lock = Lock()
_stats: Optional[Dict[str, Any]] = None
_stats_path: Optional[Path] = None


# ---------------------------------------------------------------------------
# Pricing constants (approximate costs per 1K tokens)
# ---------------------------------------------------------------------------

API_PRICING: Dict[str, Dict[str, float]] = {
    "openai/gpt-4o": {"input": 0.0025, "output": 0.01},
    "openai/gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "anthropic/claude-3-5-sonnet": {"input": 0.003, "output": 0.015},
    "anthropic/claude-3-haiku": {"input": 0.00025, "output": 0.00125},
    "openrouter/auto": {"input": 0.001, "output": 0.003},
}

# Local inference costs $0 (hardware amortized)
LOCAL_COST_PER_1K = 0.0


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SlotStats:
    """Stats for a single slot."""

    slot_id: str
    requests: int = 0
    requests_failed: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    total_latency_ms: float = 0.0
    last_used: Optional[str] = None

    def avg_latency_ms(self) -> float:
        if self.requests == 0:
            return 0.0
        return self.total_latency_ms / self.requests


@dataclass
class FailoverStats:
    """Stats for failover events."""

    total_failovers: int = 0
    by_reason: Dict[str, int] = field(default_factory=dict)
    by_slot: Dict[str, int] = field(default_factory=dict)  # slot_id -> count
    last_failover_at: Optional[str] = None


@dataclass
class DailyStats:
    """Stats for a single day."""

    date: str  # YYYY-MM-DD
    requests: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    savings_usd: float = 0.0


# ---------------------------------------------------------------------------
# Default stats structure
# ---------------------------------------------------------------------------

def _default_stats() -> Dict[str, Any]:
    return {
        "version": 1,
        "total_requests": 0,
        "total_tokens_input": 0,
        "total_tokens_output": 0,
        "total_savings_usd": 0.0,
        "slots": {},  # slot_id -> SlotStats dict
        "failovers": FailoverStats().__dict__,
        "daily": {},  # YYYY-MM-DD -> DailyStats dict
        "hourly_requests": [0] * 24,  # Last 24h distribution
        "first_request_at": None,
        "last_request_at": None,
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _get_stats_path(plugin_dir: Optional[str | Path] = None) -> Path:
    global _stats_path
    if _stats_path is not None:
        return _stats_path

    if plugin_dir:
        _stats_path = Path(plugin_dir) / "data" / "stats.json"
    else:
        # Default: relative to this file
        _stats_path = Path(__file__).resolve().parent.parent / "data" / "stats.json"

    return _stats_path


def load_stats(plugin_dir: Optional[str | Path] = None) -> Dict[str, Any]:
    """Load stats from disk, or create defaults."""
    global _stats
    with _lock:
        if _stats is not None:
            return _stats

        path = _get_stats_path(plugin_dir)
        if path.exists():
            try:
                _stats = json.loads(path.read_text(encoding="utf-8"))
                # Merge any missing keys from defaults
                defaults = _default_stats()
                for k, v in defaults.items():
                    if k not in _stats:
                        _stats[k] = v
                logger.info(f"Loaded stats from {path}")
                return _stats
            except Exception as e:
                logger.warning(f"Failed to load stats, starting fresh: {e}")

        _stats = _default_stats()
        return _stats


def save_stats() -> None:
    """Persist stats to disk."""
    with _lock:
        if _stats is None:
            return
        path = _get_stats_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(_stats, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to save stats: {e}")


# ---------------------------------------------------------------------------
# Recording methods
# ---------------------------------------------------------------------------

def record_request(
    slot_id: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_ms: float = 0.0,
    failed: bool = False,
    api_baseline: Optional[str] = None,  # e.g., "openai/gpt-4o" for savings calc
) -> None:
    """Record a request to a slot."""
    stats = load_stats()
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    today_str = now.strftime("%Y-%m-%d")

    with _lock:
        # Update totals
        if not failed:
            stats["total_requests"] += 1
            stats["total_tokens_input"] += tokens_in
            stats["total_tokens_output"] += tokens_out
            stats["last_request_at"] = now_str
            if stats["first_request_at"] is None:
                stats["first_request_at"] = now_str
        
        # Update slot stats
        if slot_id not in stats["slots"]:
            stats["slots"][slot_id] = asdict(SlotStats(slot_id=slot_id))
        
        slot_stats = stats["slots"][slot_id]
        if failed:
            slot_stats["requests_failed"] += 1
        else:
            slot_stats["requests"] += 1
            slot_stats["tokens_input"] += tokens_in
            slot_stats["tokens_output"] += tokens_out
            slot_stats["total_latency_ms"] += latency_ms
            slot_stats["last_used"] = now_str
        
        # Update hourly distribution
        hour = now.hour
        stats["hourly_requests"][hour] += 1 if not failed else 0
        
        # Update daily stats
        if today_str not in stats["daily"]:
            stats["daily"][today_str] = asdict(DailyStats(date=today_str))
        
        daily = stats["daily"][today_str]
        if not failed:
            daily["requests"] += 1
            daily["tokens_input"] += tokens_in
            daily["tokens_output"] += tokens_out
        
        # Calculate savings if baseline provided
        if api_baseline and not failed:
            baseline_price = API_PRICING.get(api_baseline, API_PRICING["openrouter/auto"])
            input_cost = (tokens_in / 1000) * baseline_price["input"]
            output_cost = (tokens_out / 1000) * baseline_price["output"]
            saved = input_cost + output_cost
            stats["total_savings_usd"] += saved
            daily["savings_usd"] += saved
    
    # Save periodically (every 5 requests)
    if stats["total_requests"] % 5 == 0:
        save_stats()


def record_failover(
    from_slot: str,
    to_slot: str,
    reason: str,
) -> None:
    """Record a failover event."""
    stats = load_stats()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with _lock:
        failover_stats = stats.get("failovers", {})
        failover_stats["total_failovers"] = failover_stats.get("total_failovers", 0) + 1
        failover_stats["last_failover_at"] = now_str
        
        # By reason
        by_reason = failover_stats.get("by_reason", {})
        by_reason[reason] = by_reason.get(reason, 0) + 1
        failover_stats["by_reason"] = by_reason
        
        # By slot
        by_slot = failover_stats.get("by_slot", {})
        by_slot[from_slot] = by_slot.get(from_slot, 0) + 1
        failover_stats["by_slot"] = by_slot
        
        stats["failovers"] = failover_stats
    
    save_stats()


# ---------------------------------------------------------------------------
# Summary methods
# ---------------------------------------------------------------------------

def get_stats_summary(window: str = "24h") -> Dict[str, Any]:
    """
    Return a summary dict for the WebUI.
    
    Args:
        window: "24h", "7d", or "30d"
    """
    stats = load_stats()
    now = datetime.now(timezone.utc)

    # Calculate window
    if window == "24h":
        since = now - timedelta(hours=24)
    elif window == "7d":
        since = now - timedelta(days=7)
    elif window == "30d":
        since = now - timedelta(days=30)
    else:
        since = now - timedelta(hours=24)
    
    # Aggregate slot stats for window (approximate from daily)
    total_requests_window = 0
    total_savings_window = 0.0
    
    for date_str, daily in stats.get("daily", {}).items():
        try:
            date = datetime.strptime(date_str, "%Y-%m-%d")
            if date >= since:
                total_requests_window += daily.get("requests", 0)
                total_savings_window += daily.get("savings_usd", 0.0)
        except ValueError:
            continue
    
    # Slot breakdown
    slots_summary = []
    for slot_id, slot_data in stats.get("slots", {}).items():
        slots_summary.append({
            "slot_id": slot_id,
            "requests": slot_data.get("requests", 0),
            "requests_failed": slot_data.get("requests_failed", 0),
            "tokens_input": slot_data.get("tokens_input", 0),
            "tokens_output": slot_data.get("tokens_output", 0),
            "avg_latency_ms": round(slot_data.get("total_latency_ms", 0) / max(slot_data.get("requests", 1), 1), 2),
            "last_used": slot_data.get("last_used"),
        })
    
    # Failover summary
    failover_data = stats.get("failovers", {})
    failover_summary = {
        "total": failover_data.get("total_failovers", 0),
        "by_reason": failover_data.get("by_reason", {}),
        "by_slot": failover_data.get("by_slot", {}),
        "last_at": failover_data.get("last_failover_at"),
    }
    
    # Hourly distribution (last 24h)
    hourly = stats.get("hourly_requests", [0] * 24)
    
    return {
        "window": window,
        "total_requests": stats.get("total_requests", 0),
        "total_requests_window": total_requests_window,
        "total_tokens_input": stats.get("total_tokens_input", 0),
        "total_tokens_output": stats.get("total_tokens_output", 0),
        "total_savings_usd": round(stats.get("total_savings_usd", 0.0), 2),
        "total_savings_window": round(total_savings_window, 2),
        "slots": slots_summary,
        "failovers": failover_summary,
        "hourly_distribution": hourly,
        "first_request_at": stats.get("first_request_at"),
        "last_request_at": stats.get("last_request_at"),
    }


def get_slot_stats(slot_id: str) -> Optional[Dict[str, Any]]:
    """Get detailed stats for a specific slot."""
    stats = load_stats()
    slot_data = stats.get("slots", {}).get(slot_id)
    if slot_data:
        return {
            "slot_id": slot_id,
            "requests": slot_data.get("requests", 0),
            "requests_failed": slot_data.get("requests_failed", 0),
            "tokens_input": slot_data.get("tokens_input", 0),
            "tokens_output": slot_data.get("tokens_output", 0),
            "avg_latency_ms": round(slot_data.get("total_latency_ms", 0) / max(slot_data.get("requests", 1), 1), 2),
            "last_used": slot_data.get("last_used"),
        }
    return None


def reset_stats(confirm: bool = False) -> bool:
    """Reset all stats (use with caution!)."""
    global _stats
    if not confirm:
        return False
    
    with _lock:
        _stats = _default_stats()
        save_stats()
    
    logger.info("Stats reset")
    return True

"""Budget engine — compose usage sources into per-provider budget state.

Turns ``(UpstreamConfig, usage sources, clock)`` into a normalized budget
dict per provider, plus ``compute_budget()`` which folds in local hardware
capacity. Pure functions only — this module calls the injected sources, it
never makes network calls of its own.

Two usage sources, picked per upstream:
  live      — ``codex_usage.fetch_codex_usage()`` for ``invoke == "codex_cli"``,
              the one provider with a live remaining-quota API today.
  declared  — ``usage_ledger.window_totals()`` against ``UpstreamConfig.limits``,
              for every other provider that declares rolling-window caps.
Everything else is either "tracked" (no cap) or "unknown" (live read failed
and there's no declared fallback).
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from local_model_router.helpers import codex_usage, compute_monitor, usage_ledger
from local_model_router.upstreams.registry import UpstreamConfig, parse_window

WARN_PCT = 80.0
EXHAUSTED_PCT = 100.0
BURN_WINDOW_SECONDS = 3600


def _status_for_pct(pct: float) -> str:
    if pct >= EXHAUSTED_PCT:
        return "exhausted"
    if pct >= WARN_PCT:
        return "warn"
    return "ok"


def _live_windows(usage: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            "label": w.get("label"),
            "used_percent": w.get("used_percent"),
            "remaining_percent": w.get("remaining_percent"),
            "window_seconds": w.get("window_seconds"),
            "resets_at": w.get("resets_at"),
            "source": "live",
        }
        for w in usage.get("windows") or []
    ]


def _declared_windows(
    upstream: UpstreamConfig,
    now: float,
    ledger_totals: Callable[[str, int, float], Dict[str, int]],
) -> List[Dict[str, Any]]:
    burn_per_hour = ledger_totals(upstream.name, BURN_WINDOW_SECONDS, now)["tokens"]
    windows: List[Dict[str, Any]] = []
    for lw in upstream.limits:
        try:
            seconds = parse_window(lw.window)
        except ValueError:
            continue
        totals = ledger_totals(upstream.name, seconds, now)
        used_tokens = totals["tokens"]
        used_requests = totals["requests"]
        pct = 0.0
        remaining: Optional[int] = None
        # ponytail: 0/None on an axis means "no cap declared there", not "cap of zero" — skip it.
        if lw.max_tokens:
            remaining = max(int(lw.max_tokens) - used_tokens, 0)
            pct = max(pct, used_tokens / int(lw.max_tokens) * 100)
        if lw.max_requests:
            pct = max(pct, used_requests / int(lw.max_requests) * 100)
        exhausts_in_hours = (
            round(remaining / burn_per_hour, 1)
            if remaining is not None and burn_per_hour > 0
            else None
        )
        windows.append({
            "window": lw.window,
            "used_tokens": used_tokens,
            "max_tokens": lw.max_tokens,
            "used_requests": used_requests,
            "max_requests": lw.max_requests,
            "remaining": remaining,
            "pct": round(pct, 1),
            "burn_per_hour": burn_per_hour,
            "exhausts_in_hours": exhausts_in_hours,
            "source": "ledger",
        })
    return windows


def provider_budget(
    upstream: UpstreamConfig,
    *,
    now: Optional[float] = None,
    ledger_totals: Callable[[str, int, float], Dict[str, int]] = usage_ledger.window_totals,
    codex_usage_fn: Callable[..., Dict[str, Any]] = codex_usage.fetch_codex_usage,
) -> Dict[str, Any]:
    """Normalized budget state for one upstream provider.

    Picks a live read (Codex) over declared-limit ledger math over a bare
    "tracked, no cap" entry, in that order of preference.
    """
    now = now if now is not None else time.time()
    entry: Dict[str, Any] = {
        "provider": upstream.name,
        "kind": upstream.type,
        "invoke": upstream.invoke,
        "enabled": upstream.enabled,
    }

    if upstream.invoke == "codex_cli":
        usage = codex_usage_fn()
        if usage.get("available"):
            windows = _live_windows(usage)
            worst = max((w["used_percent"] for w in windows if w["used_percent"] is not None), default=0.0)
            entry.update({
                "status": _status_for_pct(worst),
                "source": "live",
                "plan_type": usage.get("plan_type", ""),
                "windows": windows,
            })
            return entry
        if upstream.has_declared_limits:
            windows = _declared_windows(upstream, now, ledger_totals)
            worst = max((w["pct"] for w in windows), default=0.0)
            entry.update({
                "status": _status_for_pct(worst),
                "source": "declared_estimate",
                "windows": windows,
            })
            return entry
        entry.update({
            "status": "unknown",
            "source": "none",
            "windows": [],
            "reason": usage.get("reason", ""),
        })
        return entry

    if upstream.has_declared_limits:
        windows = _declared_windows(upstream, now, ledger_totals)
        worst = max((w["pct"] for w in windows), default=0.0)
        entry.update({
            "status": _status_for_pct(worst),
            "source": "ledger",
            "windows": windows,
        })
        return entry

    entry.update({"status": "tracked", "source": "none", "windows": []})
    return entry


def _safe_provider_budget(
    upstream: UpstreamConfig,
    *,
    now: float,
    ledger_totals: Callable[[str, int, float], Dict[str, int]],
    codex_usage_fn: Callable[..., Dict[str, Any]],
) -> Dict[str, Any]:
    """provider_budget, but a broken source degrades this one entry instead of the whole call."""
    try:
        return provider_budget(upstream, now=now, ledger_totals=ledger_totals, codex_usage_fn=codex_usage_fn)
    except Exception:
        return {
            "provider": upstream.name,
            "kind": upstream.type,
            "invoke": upstream.invoke,
            "enabled": upstream.enabled,
            "status": "unknown",
            "source": "none",
            "windows": [],
            "reason": "error computing budget",
        }


def compute_budget(
    upstreams: List[UpstreamConfig],
    *,
    now: Optional[float] = None,
    scan_hardware: Callable[[], Any] = compute_monitor.scan_hardware,
    ledger_totals: Callable[[str, int, float], Dict[str, int]] = usage_ledger.window_totals,
    codex_usage_fn: Callable[..., Dict[str, Any]] = codex_usage.fetch_codex_usage,
) -> Dict[str, Any]:
    """Full budget snapshot: local hardware capacity + every provider's budget.

    Never raises — a broken hardware probe degrades ``local`` to ``{}``, and a
    broken usage source degrades that one provider entry, not the whole call.
    """
    now = now if now is not None else time.time()
    try:
        local = scan_hardware().to_dict()
    except Exception:
        local = {}

    providers = [
        _safe_provider_budget(u, now=now, ledger_totals=ledger_totals, codex_usage_fn=codex_usage_fn)
        for u in upstreams
        if u.enabled or u.has_declared_limits
    ]
    return {"ts": now, "local": local, "providers": providers}

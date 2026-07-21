"""Tests for local_model_router.helpers.budget_engine."""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_model_router.helpers import budget_engine  # noqa: E402
from local_model_router.upstreams.registry import LimitWindow, UpstreamConfig  # noqa: E402


def _upstream(**overrides) -> UpstreamConfig:
    fields = {
        "name": "ollama_cloud",
        "type": "subscription",
        "enabled": True,
        "invoke": "",
        "default_model": "",
        "limits": (),
    }
    fields.update(overrides)
    return UpstreamConfig(**fields)


def _fake_ledger_totals(tokens_by_window, requests_by_window=None):
    """Build a ledger_totals(provider_id, window_seconds, now) fake keyed by window_seconds."""
    requests_by_window = requests_by_window or {}

    def _totals(provider_id, window_seconds, now):
        return {
            "tokens": tokens_by_window.get(window_seconds, 0),
            "requests": requests_by_window.get(window_seconds, 0),
        }

    return _totals


# ---------------------------------------------------------------------------
# Declared-limit provider
# ---------------------------------------------------------------------------

def test_declared_limit_ok_status_below_warn():
    upstream = _upstream(limits=(LimitWindow(window="1d", max_tokens=1000),))
    ledger_totals = _fake_ledger_totals({86400: 100, budget_engine.BURN_WINDOW_SECONDS: 50})

    result = budget_engine.provider_budget(upstream, now=time.time(), ledger_totals=ledger_totals)

    assert result["status"] == "ok"
    assert result["source"] == "ledger"
    window = result["windows"][0]
    assert window["used_tokens"] == 100
    assert window["max_tokens"] == 1000
    assert window["remaining"] == 900
    assert window["pct"] == 10.0
    assert window["burn_per_hour"] == 50
    assert window["exhausts_in_hours"] == 18.0  # 900 remaining / 50 per hour


def test_declared_limit_warn_status_at_80_pct():
    upstream = _upstream(limits=(LimitWindow(window="1d", max_tokens=1000),))
    ledger_totals = _fake_ledger_totals({86400: 800, budget_engine.BURN_WINDOW_SECONDS: 10})

    result = budget_engine.provider_budget(upstream, now=time.time(), ledger_totals=ledger_totals)

    assert result["status"] == "warn"
    assert result["windows"][0]["pct"] == 80.0


def test_declared_limit_exhausted_status_at_100_pct():
    upstream = _upstream(limits=(LimitWindow(window="1d", max_tokens=1000),))
    ledger_totals = _fake_ledger_totals({86400: 1000, budget_engine.BURN_WINDOW_SECONDS: 10})

    result = budget_engine.provider_budget(upstream, now=time.time(), ledger_totals=ledger_totals)

    assert result["status"] == "exhausted"
    assert result["windows"][0]["remaining"] == 0


def test_declared_limit_request_only_window():
    upstream = _upstream(limits=(LimitWindow(window="5h", max_requests=10),))
    ledger_totals = _fake_ledger_totals({18000: 0}, {18000: 9})

    result = budget_engine.provider_budget(upstream, now=time.time(), ledger_totals=ledger_totals)

    window = result["windows"][0]
    assert window["used_requests"] == 9
    assert window["max_requests"] == 10
    assert window["pct"] == 90.0
    assert window["max_tokens"] is None
    assert window["remaining"] is None  # no token cap -> no token-remaining figure
    assert result["status"] == "warn"


def test_declared_limit_zero_or_none_cap_axis_skipped():
    upstream = _upstream(
        limits=(LimitWindow(window="1d", max_tokens=0, max_requests=None),)
    )
    ledger_totals = _fake_ledger_totals({86400: 500})

    result = budget_engine.provider_budget(upstream, now=time.time(), ledger_totals=ledger_totals)

    window = result["windows"][0]
    assert window["pct"] == 0.0
    assert window["remaining"] is None
    assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# Live codex provider
# ---------------------------------------------------------------------------

def test_codex_live_available_reports_ok_and_plan_type():
    upstream = _upstream(name="codex", invoke="codex_cli")

    def fake_codex_usage_fn():
        return {
            "available": True,
            "reason": "",
            "plan_type": "plus",
            "windows": [
                {"label": "5h", "used_percent": 42.0, "remaining_percent": 58.0, "window_seconds": 18000, "resets_at": "later"},
                {"label": "7d", "used_percent": 10.0, "remaining_percent": 90.0, "window_seconds": 604800, "resets_at": "later"},
            ],
        }

    result = budget_engine.provider_budget(upstream, codex_usage_fn=fake_codex_usage_fn)

    assert result["status"] == "ok"
    assert result["source"] == "live"
    assert result["plan_type"] == "plus"
    assert len(result["windows"]) == 2
    assert result["windows"][0]["source"] == "live"
    assert "reason" not in result


def test_codex_live_unavailable_falls_back_to_declared_limits():
    upstream = _upstream(
        name="codex", invoke="codex_cli", limits=(LimitWindow(window="1d", max_tokens=1000),)
    )
    ledger_totals = _fake_ledger_totals({86400: 100, budget_engine.BURN_WINDOW_SECONDS: 0})

    def fake_codex_usage_fn():
        return {"available": False, "reason": "auth file missing or unreadable", "windows": []}

    result = budget_engine.provider_budget(
        upstream, now=time.time(), ledger_totals=ledger_totals, codex_usage_fn=fake_codex_usage_fn
    )

    assert result["status"] == "ok"
    assert result["source"] == "declared_estimate"
    assert result["windows"][0]["used_tokens"] == 100


def test_codex_live_unavailable_no_declared_limits_is_unknown_with_safe_reason():
    upstream = _upstream(name="codex", invoke="codex_cli")

    def fake_codex_usage_fn():
        return {"available": False, "reason": "auth file missing or unreadable", "windows": []}

    result = budget_engine.provider_budget(upstream, codex_usage_fn=fake_codex_usage_fn)

    assert result["status"] == "unknown"
    assert result["source"] == "none"
    assert result["windows"] == []
    assert result["reason"] == "auth file missing or unreadable"
    # The reason must be the static, non-sensitive string codex_usage produces —
    # never a token, header value, or other credential material.
    for leaky in ("Bearer", "eyJ", "access_token", "refresh_token"):
        assert leaky not in result["reason"]


# ---------------------------------------------------------------------------
# No limits at all
# ---------------------------------------------------------------------------

def test_no_declared_limits_and_not_codex_is_tracked():
    upstream = _upstream(limits=())

    result = budget_engine.provider_budget(upstream)

    assert result == {
        "provider": "ollama_cloud",
        "kind": "subscription",
        "invoke": "",
        "enabled": True,
        "status": "tracked",
        "source": "none",
        "windows": [],
    }


# ---------------------------------------------------------------------------
# compute_budget
# ---------------------------------------------------------------------------

class _FakeSnapshot:
    def to_dict(self):
        return {"available": True, "gpus": [], "cpu": {"utilization_pct": 5.0}}


def test_compute_budget_shape_with_mixed_providers():
    upstreams = [
        _upstream(name="local_free", enabled=False, limits=()),  # dropped: disabled + no limits
        _upstream(name="ollama_cloud", enabled=True, limits=(LimitWindow(window="1d", max_tokens=1000),)),
        _upstream(name="codex", enabled=True, invoke="codex_cli"),
    ]
    ledger_totals = _fake_ledger_totals({86400: 10, budget_engine.BURN_WINDOW_SECONDS: 1})

    def fake_codex_usage_fn():
        return {"available": False, "reason": "auth file missing or unreadable", "windows": []}

    result = budget_engine.compute_budget(
        upstreams,
        now=1000.0,
        scan_hardware=lambda: _FakeSnapshot(),
        ledger_totals=ledger_totals,
        codex_usage_fn=fake_codex_usage_fn,
    )

    assert result["ts"] == 1000.0
    assert result["local"] == {"available": True, "gpus": [], "cpu": {"utilization_pct": 5.0}}
    provider_names = [p["provider"] for p in result["providers"]]
    assert provider_names == ["ollama_cloud", "codex"]  # disabled/no-limits upstream dropped


def test_compute_budget_hardware_probe_failure_degrades_to_empty_local():
    upstream = _upstream(limits=(LimitWindow(window="1d", max_tokens=1000),))
    ledger_totals = _fake_ledger_totals({86400: 10, budget_engine.BURN_WINDOW_SECONDS: 1})

    def raising_scan_hardware():
        raise RuntimeError("nvidia-smi exploded")

    result = budget_engine.compute_budget(
        [upstream], scan_hardware=raising_scan_hardware, ledger_totals=ledger_totals
    )

    assert result["local"] == {}
    assert len(result["providers"]) == 1


# ---------------------------------------------------------------------------
# parse_window reuse / empty-limits robustness
# ---------------------------------------------------------------------------

def test_empty_limits_tuple_does_not_crash():
    upstream = _upstream(limits=())

    result = budget_engine.provider_budget(upstream, ledger_totals=_fake_ledger_totals({}))

    assert result["status"] == "tracked"
    assert result["windows"] == []

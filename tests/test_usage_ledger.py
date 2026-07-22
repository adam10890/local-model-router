"""Tests for local_model_router.helpers.usage_ledger."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_model_router.helpers import usage_ledger  # noqa: E402


def _isolated_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(usage_ledger, "LEDGER_PATH", tmp_path / "usage_ledger.jsonl")
    usage_ledger._cache.clear()
    monkeypatch.setattr(usage_ledger, "_last_prune_day", None)


def _write_event(path, ts, provider_id="ollama", tokens_in=10, tokens_out=0, requests=1):
    event = {
        "ts": ts,
        "provider_id": provider_id,
        "model": "m",
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "requests": requests,
        "source": "test",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def test_record_usage_appends_jsonl(monkeypatch, tmp_path):
    _isolated_ledger(monkeypatch, tmp_path)
    usage_ledger.record_usage("ollama", tokens_in=100, tokens_out=50, model="llama3", source="test")

    lines = usage_ledger.LEDGER_PATH.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["provider_id"] == "ollama"
    assert event["tokens_in"] == 100
    assert event["tokens_out"] == 50
    assert event["requests"] == 1
    assert event["model"] == "llama3"
    assert event["source"] == "test"
    assert isinstance(event["ts"], float)


def test_window_totals_rolling_windows(monkeypatch, tmp_path):
    _isolated_ledger(monkeypatch, tmp_path)
    now = time.time()
    # 1h and 3h ago: inside every window below.
    # 20h ago: inside 1d/7d, outside 5h.
    # 3d ago: inside 7d only.
    # 10d ago: outside all three windows.
    _write_event(usage_ledger.LEDGER_PATH, now - 1 * 3600, tokens_in=10)
    _write_event(usage_ledger.LEDGER_PATH, now - 3 * 3600, tokens_in=20)
    _write_event(usage_ledger.LEDGER_PATH, now - 20 * 3600, tokens_in=40)
    _write_event(usage_ledger.LEDGER_PATH, now - 3 * 86400, tokens_in=80)
    _write_event(usage_ledger.LEDGER_PATH, now - 10 * 86400, tokens_in=160)

    assert usage_ledger.window_totals("ollama", 5 * 3600, now=now) == {"tokens": 30, "requests": 2}
    assert usage_ledger.window_totals("ollama", 86400, now=now) == {"tokens": 70, "requests": 3}
    assert usage_ledger.window_totals("ollama", 7 * 86400, now=now) == {"tokens": 150, "requests": 4}


def test_events_outside_window_excluded(monkeypatch, tmp_path):
    _isolated_ledger(monkeypatch, tmp_path)
    now = time.time()
    _write_event(usage_ledger.LEDGER_PATH, now - 30, tokens_in=5, requests=1)
    _write_event(usage_ledger.LEDGER_PATH, now - 7200, tokens_in=500, requests=9)

    assert usage_ledger.window_totals("ollama", 60, now=now) == {"tokens": 5, "requests": 1}


def test_window_totals_ignores_other_providers(monkeypatch, tmp_path):
    _isolated_ledger(monkeypatch, tmp_path)
    now = time.time()
    _write_event(usage_ledger.LEDGER_PATH, now - 10, provider_id="ollama", tokens_in=5)
    _write_event(usage_ledger.LEDGER_PATH, now - 10, provider_id="other", tokens_in=999)

    assert usage_ledger.window_totals("ollama", 3600, now=now) == {"tokens": 5, "requests": 1}


def test_prune_drops_events_older_than_35_days(monkeypatch, tmp_path):
    _isolated_ledger(monkeypatch, tmp_path)
    now = time.time()
    _write_event(usage_ledger.LEDGER_PATH, now - 40 * 86400, tokens_in=1)  # older than cap
    _write_event(usage_ledger.LEDGER_PATH, now - 10 * 86400, tokens_in=2)  # kept

    usage_ledger.prune()

    lines = usage_ledger.LEDGER_PATH.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["tokens_in"] == 2


def test_prune_respects_custom_max_age(monkeypatch, tmp_path):
    _isolated_ledger(monkeypatch, tmp_path)
    now = time.time()
    _write_event(usage_ledger.LEDGER_PATH, now - 2 * 86400, tokens_in=1)
    _write_event(usage_ledger.LEDGER_PATH, now - 100, tokens_in=2)

    usage_ledger.prune(max_age_days=1)

    lines = usage_ledger.LEDGER_PATH.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["tokens_in"] == 2


def test_malformed_and_blank_lines_skipped(monkeypatch, tmp_path):
    _isolated_ledger(monkeypatch, tmp_path)
    now = time.time()
    path = usage_ledger.LEDGER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n")
        f.write("not json at all\n")
        f.write("   \n")
        f.write(json.dumps({"ts": now, "provider_id": "ollama", "tokens_in": 7, "requests": 1}) + "\n")

    assert usage_ledger.window_totals("ollama", 60, now=now) == {"tokens": 7, "requests": 1}


def test_cache_refreshes_after_new_write(monkeypatch, tmp_path):
    _isolated_ledger(monkeypatch, tmp_path)
    usage_ledger.record_usage("ollama", tokens_in=1, requests=1)
    assert len(usage_ledger.all_events()) == 1

    usage_ledger.record_usage("ollama", tokens_in=2, requests=1)
    assert len(usage_ledger.all_events()) == 2

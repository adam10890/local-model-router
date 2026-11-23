"""Test bootstrap for local-model-router.

Makes two import styles work without installation:
  - ``import local_model_router...`` (repo root on sys.path)
  - bare helper imports like ``from context_planner import ...``
    (package helpers dir on sys.path, supporting standalone helper imports)
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPERS_ROOT = REPO_ROOT / "local_model_router" / "helpers"

for _path in (REPO_ROOT, HELPERS_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


import pytest


@pytest.fixture(autouse=True)
def _hermetic_llama_process_scan(monkeypatch):
    """HTTP tests must not read the operator's live llama-server processes."""
    monkeypatch.setattr(
        "local_model_router.service.app.inspect_running_llama_models",
        lambda: {},
    )


@pytest.fixture(autouse=True)
def _hermetic_state_paths(monkeypatch, tmp_path):
    """Keep tests out of real host state directories and repo data/."""
    from local_model_router.helpers import stats_tracker, usage_ledger

    monkeypatch.setenv("A0_FLEET_STATE_DB", str(tmp_path / "fleet_state.sqlite3"))
    monkeypatch.setenv("A0_LMM_ROUTER_STATS_PATH", str(tmp_path / "failover_stats.json"))
    monkeypatch.setenv("IMPERIUM_USAGE_LEDGER_PATH", str(tmp_path / "usage_ledger.jsonl"))
    monkeypatch.setenv("A0_USAGE_LEDGER_PATH", str(tmp_path / "usage_ledger.jsonl"))
    monkeypatch.setattr(stats_tracker, "_stats", None)
    monkeypatch.setattr(stats_tracker, "_stats_path", None)
    monkeypatch.setattr(usage_ledger, "LEDGER_PATH", tmp_path / "usage_ledger.jsonl")
    usage_ledger._cache.clear()
    monkeypatch.setattr(usage_ledger, "_last_prune_day", None)

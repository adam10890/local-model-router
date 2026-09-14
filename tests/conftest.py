"""Test bootstrap for local-model-router.

Makes two import styles work without installation:
  - ``import local_model_router...`` (repo root on sys.path)
  - bare helper imports like ``from context_planner import ...``
    (package helpers dir on sys.path, matching the original plugin tests)
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

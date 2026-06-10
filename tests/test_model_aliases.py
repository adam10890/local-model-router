"""Tests for the model alias resolution layer."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_model_router.routing.aliases import (  # noqa: E402
    AliasResolution,
    public_aliases,
    resolve_alias,
)


def test_stable_aliases_map_to_roles():
    assert resolve_alias("chat").role == "chat"
    assert resolve_alias("deep").role == "chat"
    assert resolve_alias("fast").role == "utility"
    assert resolve_alias("coder").role == "utility"
    assert resolve_alias("utility").role == "utility"
    assert resolve_alias("embedding").role == "embed"
    assert resolve_alias("embeddings").role == "embed"
    assert resolve_alias("scribe").role == "scribe"


def test_aliases_are_case_insensitive():
    assert resolve_alias("CODER").role == "utility"
    assert resolve_alias("  Deep ").role == "chat"


def test_auto_routes_by_task_type():
    assert resolve_alias("auto", task_type="chat").role == "chat"
    assert resolve_alias("auto", task_type="coding").role == "utility"
    assert resolve_alias("auto", task_type="classification").role == "utility"
    assert resolve_alias("auto", task_type="embedding").role == "embed"
    assert resolve_alias("auto").is_auto is True


def test_empty_model_behaves_like_auto():
    resolution = resolve_alias(None, task_type="research")
    assert resolution.is_auto is True
    assert resolution.role == "utility"
    assert resolution.requested == "auto"


def test_unrecognized_model_passes_through():
    resolution = resolve_alias("gemma-4-12b-it-Q4_K_M")
    assert resolution == AliasResolution(
        requested="gemma-4-12b-it-Q4_K_M", role=None, recognized=False
    )


def test_public_aliases_include_auto():
    table = public_aliases()
    assert table["auto"] == "task-dependent"
    assert table["coder"] == "utility"
    # the function must not mutate the module table
    table["auto"] = "mutated"
    assert public_aliases()["auto"] == "task-dependent"

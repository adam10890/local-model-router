"""Tests for max-feasible context planning."""
from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "local_model_router"
HELPERS_ROOT = PLUGIN_ROOT / "helpers"
if str(HELPERS_ROOT) not in sys.path:
    sys.path.insert(0, str(HELPERS_ROOT))

from context_planner import (  # noqa: E402
    kv_cache_gb,
    plan_model_context,
    render_preset,
)


_SMALL_MODEL_META = {
    "n_ctx_train": 131072,
    "n_layer": 18,
    "n_embd": 2048,
    "file_size_gb": 2.0,
}


def test_min_context_is_not_a_cap_when_vram_allows_more():
    plan = plan_model_context(
        alias="chat",
        role="chat",
        model_path="/models/chat.gguf",
        min_ctx=65536,
        available_vram_gb=24.0,
        cache_type_k="q8_0",
        cache_type_v="q8_0",
        metadata=_SMALL_MODEL_META,
    )

    assert plan.no_capacity is False
    assert plan.min_ctx == 65536
    assert plan.hard_ctx > plan.min_ctx
    assert plan.hard_ctx <= 131072
    assert plan.effective_ctx == int(plan.hard_ctx * 0.70)


def test_reports_no_capacity_when_model_cannot_meet_minimum():
    plan = plan_model_context(
        alias="chat",
        role="chat",
        model_path="/models/huge.gguf",
        min_ctx=65536,
        available_vram_gb=8.0,
        metadata={
            "n_ctx_train": 131072,
            "n_layer": 80,
            "n_embd": 8192,
            "file_size_gb": 30.0,
        },
    )

    assert plan.no_capacity is True
    assert "not enough VRAM" in plan.reason


def test_kv_quantization_reduces_planned_memory():
    f16 = kv_cache_gb(65536, 32, 4096, cache_type_k="f16", cache_type_v="f16")
    q8 = kv_cache_gb(65536, 32, 4096, cache_type_k="q8_0", cache_type_v="q8_0")

    assert q8 is not None
    assert f16 is not None
    assert q8 < f16


def test_render_preset_uses_hyphenated_llama_options():
    plan = plan_model_context(
        alias="utility",
        role="utility",
        model_path="/models/utility.gguf",
        min_ctx=16384,
        available_vram_gb=24.0,
        metadata=_SMALL_MODEL_META,
    )

    preset = render_preset(
        [plan],
        global_options={"cache-type-k": "q8_0", "flash-attn": "on"},
    )

    assert "[*]" in preset
    assert "[utility]" in preset
    assert "ctx-size = " in preset
    assert "cache-type-k = q8_0" in preset
    assert "flash-attn = on" in preset

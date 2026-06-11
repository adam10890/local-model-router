# DOX contract — local_model_router/cookbook

## Purpose

Hardware-aware model recommendations ("which model should serve each
role?"). Inspired by whichllm (github.com/Andyyyy64/whichllm): VRAM fit
modeling with evidence-graded confidence — adapted to this router, where
models are already on disk, roles come from the fleet config, and a
recommendation can be applied by starting a slot.

## Ownership

- `gguf.py` — minimal GGUF header reader. Metadata key-values only; stops
  at the tokenizer block so large files cost a few KB of I/O. Also owns
  quant→bits tables and shard-name helpers.
- `engine.py` — pure engine: scan a models dir (role subfolders are role
  hints), fit math (weights + KV cache + overhead vs VRAM budget), 0-100
  role scores, per-role recommendations. No HTTP, no config I/O — callers
  pass hardware/context_policy dicts.
- The HTTP endpoint (`GET /cookbook`) lives in `service/app.py`; it reads
  `LLAMA_MODELS_DIR` env or `global.models_dir` from the fleet YAML and
  caches the report for 60s.

## Local Contracts

- Every assessment carries `reasons[]` and a `confidence` grade
  (`high` = GGUF metadata parsed, `medium` = estimated, `low` = guessed).
  Never emit a bare score.
- KV-cache math: `2 * layers * kv_heads * head_dim * 2 bytes` per token;
  fall back to 0.125 MiB/token (with a reason) when geometry is unknown.
- An explicit role-folder hint pins a model to that role; unlabeled models
  are never recommended as embedders.
- The engine must stay dependency-free (stdlib only) and never load tensor
  data.

## Verification

- `python -m pytest tests/test_cookbook.py -q`

## Child DOX Index

None.

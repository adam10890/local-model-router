# 49 — Streaming SSE token usage accounting and opt-in Hermes GPU swap wait

- Version: `0.15.0`
- Branch: `dev/streaming-usage-and-swap-wait`
- Proposed ADR: `docs/adr/0021-opt-in-bounded-request-wait-during-hermes-gpu-swap.md`

## Summary

1. Added `SseUsageExtractor` (`local_model_router/service/sse_usage.py`) with a bounded 64 KiB line buffer storing only numeric token counts and wired it into local (`stream_options: {"include_usage": True}` injected when absent) and upstream SSE streams so streaming usage lands in `FleetStore` and `usage_ledger`.
2. Updated streaming cleanup in `routes_openai.py` to honor `should_close_session` from `SharedHttpClient.open_stream_session`.
3. Added opt-in bounded swap waiting (`HermesModelWatcher.wait_while_swapping()`, `swap_wait_seconds`, `IMPERIUM_SWAP_WAIT_SECONDS` with `A0_LMM_ROUTER_SWAP_WAIT_SECONDS` fallback, default `0.0`, max `60.0`) waking via `asyncio.Event` on phase/task completion, emitting `x-a0-router-swap-wait-ms`, and failing fast on `backoff`, `gpu_leased_by_studio`, `gpu_slot_stopped`, and `gpu_swap_failed`.
4. Added Proposed ADR-0021, `tests/test_sse_usage.py`, and swap-wait tests in `tests/test_gpu_swap_api.py`.

## Verification

- `.\.venv\Scripts\ruff.exe check local_model_router tests scripts tools`
- `.\.venv\Scripts\python.exe tools/adr.py lint --base origin/main`
- `.\.venv\Scripts\python.exe -m compileall -q local_model_router tests scripts/smoke_harnesses.py tools/adr.py`
- `.\.venv\Scripts\coverage.exe run --source=local_model_router -m pytest tests/ --ignore=tests/test_branch_guard.py -q` + 4 coverage gates
- `git diff --check`

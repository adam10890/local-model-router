# 51 — Stream timeout and router-requested usage chunk

- Version: `0.16.1`
- Branch: `dev/stream-timeout-and-usage-chunk`
- Proposed ADR: none (bug fix; no architectural commitment changes)

## Summary

1. Added `_STREAM_TIMEOUT` (`local_model_router/service/context.py`): streaming
   forwards use `total=None` with `sock_connect`/`sock_read` at the forward
   timeout, so a long generation is bounded by silence, not total length.
   A read stall still raises `aiohttp.ServerTimeoutError`, which the stream
   helper already turns into an `upstream_stream_error` event.
2. Added `request_stream_usage()` and `UsageOnlyEventFilter`
   (`local_model_router/service/sse_usage.py`). Both local and upstream
   streaming paths in `routes_openai.py` request `include_usage` only when the
   client did not set `stream_options`, and then drop the usage-only event
   from the client stream after the extractor has counted it.
3. Upstream streams now request usage as well (reverses the 0.15.0 choice not
   to touch `stream_options` on upstreams), closing the token-economy
   follow-up "streaming upstream usage not parsed".

## Verification

- `ruff check local_model_router tests scripts tools`
- `.\.venv\Scripts\python.exe tools/adr.py lint`
- `.\.venv\Scripts\python.exe -m pytest tests/ --ignore=tests/test_branch_guard.py -q`

## Risk

An `openai_compatible` upstream that rejects unknown `stream_options` would
now fail streaming requests. Ollama, vLLM, LiteLLM, LM Studio, and llama.cpp
accept it; not verified against live providers.

# 47 — Service modularization, shared HTTP client pool, and legacy orchestrator sunset

- Version: `0.14.3`
- Branch: `dev/service-modularization`
- Proposed ADR: `docs/adr/0020-isolate-and-schedule-removal-of-legacy-orchestrator-api.md`

## Summary

1. Introduced `SharedHttpClient` (`local_model_router/service/http_client.py`) with a pooled `TCPConnector` per running loop, clean lifespan shutdown, and automatic fallback whenever `aiohttp.ClientSession` is monkeypatched in tests.
2. Wired `SharedHttpClient` across OpenAI forwarding/streaming/embeddings, `models_listing.py`, `SlotHealthChecker` (`health.py`), and `mcp/router_bridge.py` + `mcp/server.py` lifespan.
3. Split the monolithic `local_model_router/service/app.py` into `context.py`, `routes_setup.py`, `routes_fleet.py`, `routes_harnesses.py`, `routes_orchestrator.py`, `routes_openai.py`, and a slim `app.py` factory preserving monkeypatch re-exports.
4. Gated `/orchestrator/*` and default `AgentOrchestrator` instantiation behind `IMPERIUM_ENABLE_LEGACY_ORCHESTRATOR` (fallback `A0_LMM_ROUTER_ENABLE_LEGACY_ORCHESTRATOR`, default `"1"`), emitted `Sunset: Wed, 31 Dec 2026 23:59:59 GMT`, documented the policy in `docs/future-orchestration.md`, and added Proposed ADR-0020.

## Verification

- `.\.venv\Scripts\ruff.exe check local_model_router tests scripts tools`
- `.\.venv\Scripts\python.exe tools/adr.py lint --base origin/main`
- `.\.venv\Scripts\python.exe -m compileall -q local_model_router tests scripts/smoke_harnesses.py tools/adr.py`
- `.\.venv\Scripts\coverage.exe run --source=local_model_router -m pytest tests/ --ignore=tests/test_branch_guard.py -q` + 4 coverage gates (`75%`, `70%`, `70%`, `70%`)
- `git diff --check`

# 50 — Harness pin-drift alert, MCP harness resources, and OpenAPI 3.1.0 schema

- Version: `0.16.0`
- Branch: `dev/pin-drift-openapi-mcp-harnesses`

## Summary

1. Added `harness_pin_drift` optional configuration alert to `build_ui_status()` (`local_model_router/service/readiness.py`) and `_collect_readiness()` (`local_model_router/service/routes_setup.py`), pointing operators to `#/connections` when a pinned harness connection targets an unservable model without downgrading `overall: ready`.
2. Exposed read-only MCP harness resources `harnesses://list` and `harnesses://{harness_id}/setup` (`local_model_router/mcp/resources.py` and `router_bridge.py`).
3. Added generated OpenAPI `3.1.0` schema builder (`local_model_router/service/openapi_spec.py`) mounted at protected `GET /openapi.json` (`local_model_router/service/routes_fleet.py`).
4. Added HTTP contract tests for `/openapi.json`, OpenAI `/v1/*` and `/harnesses/*/v1/*` shapes, pin-drift readiness, and secret/path sanitization (`tests/test_openapi_and_contract.py`), plus MCP harness resource unit tests (`tests/test_mcp_bridge.py`).

## Verification

- `.\.venv\Scripts\ruff.exe check local_model_router tests scripts tools`
- `.\.venv\Scripts\python.exe tools/adr.py lint --base origin/main`
- `.\.venv\Scripts\python.exe -m compileall -q local_model_router tests scripts/smoke_harnesses.py tools/adr.py`
- `.\.venv\Scripts\coverage.exe run --source=local_model_router -m pytest tests/ --ignore=tests/test_branch_guard.py -q` + 4 coverage gates
- `.\.venv\Scripts\python.exe -m pytest tests_e2e/dashboard/test_dashboard.py -q`
- `git diff --check`
- `.\.venv\Scripts\python.exe scripts/branch_guard.py --queue`

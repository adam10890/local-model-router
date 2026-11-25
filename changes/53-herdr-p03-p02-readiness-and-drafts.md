# 53 — Herdr P03 & P02 maintenance: Honest readiness, generation fences, and draft continuity

- Version: `0.16.3`
- Branch: `dev/herdr`
- Queue entry: `Q3` (`docs/herdr-todo.md#p03`, `docs/herdr-todo.md#p02`, R02, R19–R21)
- Proposed ADR: none (hardening existing readiness, harness manifest, and dashboard state paths)

## Summary

1. Added monotonic `freshness` metadata (`generation`, `checked_at`, `last_ready_at`, `stale_after_seconds`) to `build_ui_status()` and `GET /ui/status` (`local_model_router/service/readiness.py`, `routes_setup.py`, `context.py`).
2. Separated `configured`, `reachable`, `completion_verified`, and `client_verified` (`evidence_level`) in `local_model_router/harnesses/setup.py` and `routes_harnesses.py`, preserving verified state across `/models` refreshes and resetting stale verification on connection repin.
3. Added request/server generation fences, disconnect staleness banners, and `visibilitychange`/`focus`/`online` revalidation in `local_model_router/dashboard/assets/app.js` + `i18n.js`, ensuring late older replies cannot restore `ready` after disconnect.
4. Preserved chat composer draft (`state.chatDraft`), active input focus and selection range, and non-bottom `#chat-messages` scroll position across `render()` updates (`P02` maintenance slice).
5. Added hermetic API/manifest tests (`tests/test_harness_api.py`, `tests/test_dashboard_static_contract.py`) and real-Chromium Playwright E2E coverage (`tests_e2e/dashboard/test_dashboard.py`), including live browser `EventSource('/setup/events')` verification.

## Verification

- `node --check local_model_router/dashboard/assets/app.js`
- `node --check local_model_router/dashboard/assets/i18n.js`
- `.\.venv\Scripts\ruff.exe check local_model_router tests scripts tools`
- `.\.venv\Scripts\python.exe tools/adr.py lint --base origin/main`
- `.\.venv\Scripts\coverage.exe run --source=local_model_router -m pytest tests/ -q` (full suite, zero `--ignore`) + 4 coverage gates
- `.\.venv\Scripts\python.exe -m pytest tests_e2e/dashboard/test_dashboard.py -q`

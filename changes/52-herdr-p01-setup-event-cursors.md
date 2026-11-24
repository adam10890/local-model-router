# 52 — Herdr P01: Monotonic setup event cursors and resync

- Version: `0.16.2`
- Branch: `dev/herdr`
- Queue entry: `Q3` (`docs/herdr-todo.md#p01`, R16–R18)
- Proposed ADR: none (bug fix on existing `/setup/events` path; no architectural commitment change)

## Summary

1. Replaced retention-length event numbering in `SetupEngine` (`local_model_router/setup/engine.py`) with a bounded `deque(maxlen=200)` and monotonic `_next_event_id`, emitting explicit `history` (`empty`/`retained`/`lost`), `resync_required`, `cursor`, `oldest_id`, and `latest_id` metadata.
2. Updated `GET /setup/events` (`local_model_router/service/routes_setup.py`) to surface `503 setup_events_unavailable` (`history: "unavailable"`, `resync_required: true`) on store failure and yield SSE `event: resync` snapshots with duplicate-free transition handling.
3. Updated dashboard polling (`local_model_router/dashboard/assets/app.js`) to track `state.eventCursor` across incremental polls and replace/resync when `resync_required` is set.
4. Added hermetic unit, boundary, gap, error, and SSE transition tests in `tests/test_setup_engine.py` and `tests/test_setup_api.py`.

## Verification

- `node --check local_model_router/dashboard/assets/app.js`
- `.\.venv\Scripts\ruff.exe check local_model_router tests scripts tools`
- `.\.venv\Scripts\python.exe tools/adr.py lint --base origin/main`
- `.\.venv\Scripts\coverage.exe run --source=local_model_router -m pytest tests/ --ignore=tests/test_branch_guard.py -q` + 4 coverage gates
- `.\.venv\Scripts\python.exe -m pytest tests_e2e/dashboard/test_dashboard.py -q`

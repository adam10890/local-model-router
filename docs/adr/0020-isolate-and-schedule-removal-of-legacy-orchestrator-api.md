# 0020. Isolate and schedule removal of legacy orchestrator API

Date: 2026-11-23

## Status

Proposed

## Context

The legacy `/orchestrator/*` HTTP surface (`agent_orchestrator.py`) duplicates Durable Task / execution-ticket state inside Imperium's router process. Per ADR-0017 (task ownership: commitments for intent, executors for execution state) and [docs/future-orchestration.md](../future-orchestration.md), Hermes is the primary client harness, dashboard references to `/orchestrator/*` are already hidden, and Imperium's 1.0 beta goals focus on local model serving, routing, harness pinning, and hardware readiness rather than embedded orchestrator workflows. Keeping `/orchestrator/*` tightly coupled inside `service/app.py` and always initialized at startup adds dead SQLite setup, route sprawl, and cognitive overhead without serving the active Hermes workflow. At the same time, immediately deleting `/orchestrator/*` in one release could break scripted callers or tests before an explicit deprecation window. This isolation choice is easily reversible by toggling one environment variable.

## Decision

We will isolate the legacy `/orchestrator/*` endpoints into `local_model_router/service/routes_orchestrator.py`, emit an explicit `Sunset: Wed, 31 Dec 2026 23:59:59 GMT` header alongside `Deprecation: true` on every response, and gate both `/orchestrator/*` route registration and default `AgentOrchestrator` initialization behind `IMPERIUM_ENABLE_LEGACY_ORCHESTRATOR` (with `A0_LMM_ROUTER_ENABLE_LEGACY_ORCHESTRATOR` fallback, defaulting to enabled `"1"` until the sunset date). Operators migrating off `/orchestrator/*` move task/commitment ownership to their external commitment tracker and Hermes harness per ADR-0017 and `docs/future-orchestration.md`.

Rejected options:
- Immediate deletion of `/orchestrator/*` and `agent_orchestrator.py`: breaks backwards compatibility without a discovery window (`Deprecation` + `Sunset`) or opt-out verification.
- Leaving `/orchestrator/*` permanently wired in `service/app.py` without a runtime toggle or sunset header: prolongs dead-surface coupling and gives callers no machine-readable shutdown date.

## Consequences

- Positive: operators can turn off the legacy orchestrator surface and avoid opening `orchestrator.sqlite3` via `IMPERIUM_ENABLE_LEGACY_ORCHESTRATOR=0`, while HTTP clients receive standard `Deprecation: true` and `Sunset` headers ahead of removal.
- Positive: extracting `routes_orchestrator.py` keeps the remaining router modules focused on active OpenAI, setup, fleet, and harness surfaces.
- Negative: callers or automation still relying on `/orchestrator/*` must migrate to Hermes plus an external commitment record (ADR-0017) or explicitly pin the legacy flag before the `2026-12-31` sunset window closes; until removal, maintaining the toggle adds one small conditional branch.
- Neutral / follow-up: confirm compliance via `tests/test_service_modularization.py` (verifying `404` when disabled and `Deprecation` + `Sunset` headers when enabled). Review trigger: after `2026-12-31` or when 1.0 beta P0 gates close, submit a follow-up PR removing `routes_orchestrator.py` and `agent_orchestrator.py`.

## Change History

| Version | Date | Author | Change |
|---|---|---|---|
| 0.1 | 2026-11-23 | agent | Initial proposal |

# PRD — Observability surface

**Status:** Proposed — not shipped behavior
**Owner:** Product / founder
**Goals served:** G5 (recoverable operation), G7 (sustainable solo delivery)
**Gate:** deferred until the 1.0 beta gates in [`../1.0-beta-evidence.md`](../1.0-beta-evidence.md) pass
**Catalogue origin:** `langfuse`, `openllmetry`, `opik`

## Problem

Imperium already measures itself. `service/fleet_manager.py` keeps four SQLite
tables — `agents`, `requests`, `queue_events`, `model_residency_snapshots` —
recording status, queue depth, admission lane, duration, fallback use, reason
codes, cache status and token counts per request. `routing_analytics()` at
`fleet_manager.py:467` aggregates them.

All of it is reachable through exactly one bespoke JSON endpoint,
`GET /routing/analytics`. Nothing in the ecosystem can read it: every tool in
the catalogue's observability section scrapes Prometheus or ingests
OpenTelemetry, and a repository-wide search for `prometheus` returns nothing.

The cost is not theoretical. The nightly Windows job failed on 2026-10-12 and
again on 2026-11-04, and diagnosing it required reading source, because the one
signal that would have named the cause was captured into a shell variable and
discarded. That specific hole is now fixed, but it is the same shape as the
general one: the system knows why, and has nowhere to say it.

This is the cheapest of the three proposals and the one that makes the other
two measurable — structured-output enforcement rate and cache-affinity hit rate
both want somewhere to land.

## Scope

**`GET /metrics`, Prometheus exposition format.** Rendered from the counters
`FleetStore` already holds. Hand-rolled text output, no new runtime dependency:
the format is a handful of lines per series, and `GOALS.md` G7 says a
dependency must remove more complexity than it introduces.

Series worth exposing, all of which already exist as data:

| Metric | Source |
| --- | --- |
| requests by status, app, routing strategy | `requests` table |
| fallback and no-slot counts | `routing_analytics()` |
| queue depth, admissions, `queue_full` rejections | `queue_events` |
| request duration and queue wait | `requests.duration_ms`, `queued_ms` |
| prompt-cache hit / miss / bypass | existing `cache_status` |
| per-upstream budget status | `helpers/budget_engine.py` |
| slot health and model residency | `model_residency_snapshots` |

**Authentication like everything else.** `/health` is the only open route;
`/metrics` honors the bearer key through the existing `protected()` wrapper. A
scraper gets a token like any other client.

**Optional tracing.** OpenTelemetry spans on the routing decision and the
forward path, behind a `[otel]` extra and off by default, so the core keeps
starting without it (G7). Span attributes are reason codes and slot ids — never
prompt text.

## Non-goals

- Shipping a dashboard, a Grafana board, or a bundled Prometheus. Imperium
  exposes; the operator scrapes.
- Any metric that carries a prompt body, a schema, a user id or machine-local
  model identity. G4 is the only goal currently rated Pass and this must not be
  what changes that. Label cardinality stays bounded: app id, status, strategy,
  lane, slot id — nothing free-form from a request.
- Replacing `/routing/analytics`. It stays; `/metrics` is an additional view of
  the same store.
- Retention or downsampling policy. The SQLite store already has its own.

## Seams it reuses

- `local_model_router/service/fleet_manager.py` — `FleetStore` and
  `routing_analytics()` at line 467; all four tables.
- `local_model_router/helpers/budget_engine.py` — `provider_budget()` and
  `compute_budget()`, already surfaced at `GET /compute/budget`.
- `local_model_router/helpers/compute_monitor.py` — the cached hardware probe
  behind `GET /fleet/status`.
- The `protected()` auth wrapper and route table in `service/app.py`.

## Open decisions

1. Is `/metrics` always on, or behind an env flag like fleet control? Always-on
   is conventional and it is authenticated; a flag is more conservative.
2. Do GPU and RAM gauges belong here, or stay in `/fleet/status`? The compute
   probe shells out to `nvidia-smi`, and a scrape every fifteen seconds is a
   different load profile than a dashboard refresh.
3. Is the OTel extra worth carrying at all for a solo operator, or is
   `/metrics` the whole need?

## Verification

- Hermetic: a seeded `FleetStore` renders a valid exposition payload; a
  Prometheus text parser accepts it.
- `/metrics` returns 401 without the bearer key when one is configured.
- A redaction test asserts no prompt body, schema, or machine-local model path
  appears in the output, extending the existing G4 suite.
- Label cardinality is bounded: a request carrying an unexpected app id does
  not create an unbounded series.
- Live: scrape a running router twice and confirm counters move as requests
  flow.

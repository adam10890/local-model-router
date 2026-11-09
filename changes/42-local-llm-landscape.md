# Added

- `docs/local-llm-landscape.md` — an ecosystem gap review mapping the
  `awesome-local-llm` catalogue onto Imperium's current surface, each row
  checked against the code rather than recalled. Records what is already
  covered, the three selected gaps, and why retrieval, safeguards and speech
  were deferred.
- `docs/prd/structured-output.md` — normalize `response_format` into each
  backend's dialect at the forward boundary, validate the reply, repair once,
  and say which of the three happened. `response_format` is currently read only
  to pick a slot and is forwarded untranslated, so whether a harness gets valid
  JSON depends on which backend routing chose.
- `docs/prd/observability.md` — `GET /metrics` in Prometheus format from the
  counters `FleetStore` already keeps, plus optional OpenTelemetry spans behind
  an extra. No new runtime dependency and no prompt bodies.
- `docs/prd/cache-aware-routing.md` — a prefix fingerprint and a bounded
  affinity term in `rank_candidates`, so a conversation returns to the slot
  that still holds its prefix warm instead of paying prompt evaluation again.

All four are `Status: Proposed — not shipped behavior` and gated behind the 1.0
beta gates; `docs/AGENTS.md` now owns them and states that rule.

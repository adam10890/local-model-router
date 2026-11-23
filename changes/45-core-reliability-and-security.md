# Core reliability, SQLite hardening, and security fixes

### Fixed

- Ran `check_structured_output()` before `finalize()` on non-streaming upstream
  forwards so enforced 422 rejections record `failed` telemetry, omit rejected
  usage from the usage ledger, and keep repairs inside the active admission
  lease.
- Wrapped upstream SSE streams in `_ManagedStreamingResponse` with `on_close`
  cleanup and scanned across 8 KiB chunk boundaries for `data: [DONE]` and
  non-null `finish_reason`.
- Routed `POST /v1/embeddings` through `local` `FleetQueue` admission,
  managed-slot swap guards, timeout/invalid-JSON handling, and `FleetStore`
  telemetry.
- Hardened `FleetStore` (`WAL`, `busy_timeout`, deterministic connection close,
  indexes, `rowid DESC` tie-breaker, bounded pruning, deduplicated
  `observer_slots` snapshots, async thread offload) and moved both `FleetStore`
  and `stats_tracker` default state files into Imperium's state directory with
  atomic writes.

### Security

- Redacted `config_path` from unauthenticated `GET /health` responses when an
  API key is configured or the caller is non-loopback.
- Returned OpenAI-formatted `401` error envelopes for `/harnesses/.../v1/...`.
- Added security headers (`CSP`, `X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`) to `/ui` and added cross-origin / simple-content-type CSRF
  guards for unauthenticated localhost mutating requests.
- Hardened `Dockerfile` (non-root user `imperium`, non-editable install,
  `HEALTHCHECK`) and pinned `/app/data` state paths in `docker-compose.yml`.

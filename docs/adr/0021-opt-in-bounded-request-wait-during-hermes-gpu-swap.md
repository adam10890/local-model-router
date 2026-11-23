# 0021. Opt-in bounded request wait during Hermes GPU swap

Date: 2026-11-23

## Status

Proposed

## Context

When Hermes switches its active session model on the shared single-GPU slot (ADR-0015), `HermesModelWatcher` swaps the managed llama.cpp process. By default Imperium returns `503 gpu_swap_in_progress` immediately with `Retry-After` so OpenAI clients can back off cleanly without tying up worker connections. However, interactive or single-request harnesses that do not implement automatic retries on `503` fail mid-turn during brief swaps (`5–15s`), even though waiting briefly on an active `gpu_swap_in_progress` transition would complete the request transparently. At the same time, waiting indefinitely or waiting during retry `backoff`, Studio leases (`gpu_leased_by_studio`), manual stops (`gpu_slot_stopped`), or permanent failures (`gpu_swap_failed`) would stall requests unnecessarily and conflict with ADR-0009 (promote latency discipline) and ADR-0018/0019 (Studio lease isolation). The change is reversible via configuration (`swap_wait_seconds: 0.0`).

## Decision

We will provide an opt-in, bounded request wait during active Hermes GPU swaps configured via `swap_wait_seconds` in `hermes_gpu_profiles.yaml` or `IMPERIUM_SWAP_WAIT_SECONDS` (`A0_LMM_ROUTER_SWAP_WAIT_SECONDS` fallback), defaulting to `0.0` seconds (fail-fast `503` preserved) and capped at `60.0` seconds. `HermesModelWatcher.wait_while_swapping()` wakes immediately on watcher phase/task transitions via per-waiter `asyncio.Event` instances and waits strictly while `retry_response()` returns `"gpu_swap_in_progress"` outside of `"backoff"` — never waiting on `gpu_leased_by_studio`, `gpu_slot_stopped`, `gpu_swap_failed`, or `backoff`. When a waited request resolves and succeeds, the response emits `x-a0-router-swap-wait-ms`.

Rejected options:
- Default-enabled unconditional waiting: risks holding client sockets and admission queues open during unexpected swap stalls for callers that expect fail-fast `503`.
- Fixed polling sleep loops inside route handlers: adds artificial tail latency instead of waking instantaneously when the swap finishes or fails.

## Consequences

- Positive: operators running non-retrying harnesses can set a small swap-wait budget so brief GPU model swaps succeed transparently on first attempt, with latency observable via `x-a0-router-swap-wait-ms`.
- Positive: Studio GPU leases, manual stops, failed swaps, and backoff windows continue to fail fast with zero delay.
- Negative: while `swap_wait_seconds > 0`, waiting HTTP requests hold their client connection (and local admission slot if acquired) open up to the configured cap, and enabling it requires opting in via config or environment variable because the default remains `0.0`.
- Neutral / follow-up: confirm compliance via unit tests in `tests/test_gpu_swap_api.py` covering default fail-fast `0.0`, mid-swap wakeup + header emission, timeout `503`, env override/cap, and zero-wait fast-fail states. Review trigger: revisit the `60s` upper bound after large-model promote telemetry is collected against ADR-0009.

## Change History

| Version | Date | Author | Change |
|---|---|---|---|
| 0.1 | 2026-11-23 | agent | Initial proposal |

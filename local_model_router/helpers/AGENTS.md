# DOX contract — local_model_router/helpers

## Purpose

Orchestration and routing layer carried over from the plugin extraction:
config resolution, context planning, slot management, failover chains, and
async health checks.

## Ownership

- `conf_resolver.py` — single source of truth for `llama_cpp_servers.yaml`
  resolution; env overrides must stay inside safe config roots.
- `llama_cpp_manager.py` — `BackendManager` slot orchestration + failover
  entry point (`select_slot_with_failover_async`).
- `backends/` — pluggable execution layer (`remote` is the production
  default; `docker`/`subprocess` exist for lifecycle-owning setups; docker
  SDK is an optional extra).
- `smart_router/` — failover chains + `SlotHealthChecker`.
- `context_planner.py` / `context_calculator.py` — max-feasible context
  planning for GGUF models.
- `stats_tracker.py` — failover event recording.

## Local Contracts

- No Agent Zero imports. Ever.
- Keep helper behavior server-agnostic; no Windows/RTX-4090 assumptions.

## Work Guidance

- New backend adapters subclass `backends/base.py:InferenceBackend` and
  register in `backends/factory.py`; represent capabilities explicitly
  rather than pretending all backends support everything.

## Verification

- `python -m pytest tests/test_select_slot_with_failover.py tests/test_select_slot_with_failover_async.py tests/test_slot_health_checker.py tests/test_conf_resolver.py -q`

## Child DOX Index

No child AGENTS.md files yet.

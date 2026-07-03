# DOX contract — local-model-router/tests

## Purpose

Hermetic pytest suite covering routing intent, observer endpoints, OpenAI
forwarding, fleet manager admission, failover, and health checks.

## Local Contracts

- Tests must pass with **no live fleet, no GPU, no network**. Stub HTTP
  (aiohttp fakes), use `tmp_path` configs, and monkeypatch context sources —
  never depend on a running llama.cpp instance.
- `conftest.py` provides both import styles (package + bare helper modules);
  do not add per-file `sys.path` hacks beyond what the ported tests carry.

## Verification

- `python -m pytest tests/ -q`
- Harness changes must include `tests/test_harness_profiles.py` and
  `tests/test_harness_api.py`; they remain fleet- and network-free.

## Child DOX Index

No child AGENTS.md files yet.

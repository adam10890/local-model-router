# Dedicated Harness Endpoints Implementation Plan

**Goal:** Give each harness a stable OpenAI-compatible base URL pinned to one compute model, with Agent Zero as the only two-connection exception.

**Architecture:** Load canonical harness connections from `conf/harnesses.yaml`. Dedicated route wrappers resolve the path to a connection, override client model/routing input with the configured target, and reuse the existing chat/model forwarding code. The dashboard consumes a setup-manifest API; config writes remain opt-in and atomic.

**Tech stack:** Python 3.10+, Starlette, PyYAML, Alpine.js, pytest.

---

## Task 1: Harness profile domain and validation

**Files:**
- Create: `local_model_router/harnesses/__init__.py`
- Create: `local_model_router/harnesses/profiles.py`
- Create: `conf/harnesses.yaml`
- Test: `tests/test_harness_profiles.py`

1. Write failing tests for single-connection harnesses, Agent Zero's named connections, invalid IDs, duplicate/empty models, and legacy `apps.yaml` fallback.
2. Implement immutable profiles, canonical YAML loading, validation errors, and legacy compatibility.
3. Run `python -m pytest tests/test_harness_profiles.py -q`.

## Task 2: Dedicated setup and OpenAI-compatible routes

**Files:**
- Modify: `local_model_router/service/app.py`
- Create: `tests/test_harness_api.py`

1. Write failing API tests for list/detail manifests, `/models`, pinned `/chat/completions`, Agent Zero connection paths, auth, unknown paths, and unavailable pinned targets.
2. Add `harnesses_path` to the app factory and load profiles.
3. Add protected list/detail/model/chat routes. Dedicated chat must ignore request model and client routing hints, attach harness identity, and pass the configured model/slot to the existing forwarding pipeline.
4. Preserve generic `/v1` behavior.
5. Run focused API and existing forwarding tests.

## Task 3: Opt-in atomic harness creation

**Files:**
- Modify: `local_model_router/harnesses/profiles.py`
- Modify: `local_model_router/service/app.py`
- Modify: `tests/test_harness_profiles.py`
- Modify: `tests/test_harness_api.py`

1. Write failing tests for write-disabled behavior, validation, atomic replacement, and backup creation.
2. Implement `A0_LMM_ROUTER_ENABLE_CONFIG_WRITES=1` gating and atomic YAML updates without secrets.
3. Return the generated setup manifest after creation.

## Task 4: Harness-first dashboard

**Files:**
- Modify: `local_model_router/dashboard/index.html`
- Modify: `tests/test_phase_e_surfaces.py`
- Modify: `tests/test_local_first_plus_api.py`

1. Update HTML assertions first: one Harnesses tab, connection cards, exact URLs/model, Copy/Verify, setup output, and Add Harness preview/save states.
2. Replace the legacy per-role matrix and generic “Connect an agent” screen with the harness-first screen backed by `/harnesses`.
3. Keep the generic API instructions in documentation, not as the primary dashboard contract.
4. Run dashboard/API surface tests.

## Task 5: Documentation and release governance

**Files:**
- Modify: `README.md`
- Rewrite: `docs/HARNESSES.md`
- Modify: `docs/INTEGRATIONS.md`
- Modify: nearest `AGENTS.md` ownership docs
- Modify: `local_model_router/__init__.py`
- Modify: `CHANGELOG.md`
- Create: `changes/<pr>-dedicated-harness-endpoints.md`

1. Document the terminology, endpoint contract, new-harness setup, Agent Zero exception, Hermes/Pi instructions, and optional Claude Code via LiteLLM.
2. Classify as additive minor release and bump to `0.4.0`.
3. Add changelog and release fragment after the PR number is known.

## Task 6: Verification, PR, and merge

1. Run `python -m py_compile` on every touched Python file.
2. Run `python -m pytest tests/ -q` with no fleet.
3. Run `git diff --check`, inspect scope and search for secrets/prompt bodies.
4. Start the router against the local config and smoke `/health`, `/ui`, harness manifests, and live Hermes/Pi completion when their configured compute is available.
5. Rename branch to `ready/harness-endpoints`, commit explicit files, push, open a ready PR, wait for checks, squash-merge, and verify `origin/main`.

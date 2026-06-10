# DOX contract — local-model-router (repo root)

## Purpose

Standalone local-first model router. One OpenAI-compatible gateway in front
of a local llama.cpp fleet: explainable intent routing, slot health/failover,
chat-completions forwarding, and a Fleet Manager control plane (agent
identity, bounded queueing, SQLite telemetry).

Extracted 2026-06-10 from the `a0_lmm_router` Agent Zero plugin
(github.com/adam10890/a0_lmm_router). The router is the product; Agent Zero
is one client. Do not reintroduce Agent Zero imports or assumptions into this
codebase.

## Ownership

- `local_model_router/service/` — HTTP surface and control plane.
- `local_model_router/helpers/` — config resolution, context planning, slot
  orchestration, failover, health.
- `conf/` — fleet description. `llama_cpp_servers.yaml` is local-only
  (gitignored); the `.example` variant is the committed reference.
- `tests/` — hermetic pytest suite; no live fleet or GPU required.
- The service must remain Docker-socket-free: it routes to running slots; it
  does not start or stop containers.

## Local Contracts

- Default bind is loopback. Public binds require an API key
  (`A0_LMM_ROUTER_API_KEY`) or an explicit no-auth acknowledgment env.
- `/health` stays open; every other endpoint honors bearer auth when a key is
  set.
- Routing decisions must stay explainable: reason codes + warnings, never
  silent fallbacks.
- Config previews and telemetry must redact secrets and never include prompt
  bodies.

## Work Guidance

- Keep increments small and behavior-preserving; this codebase is trusted by
  a live setup.
- Phase roadmap: (1) `GET /v1/models` + model aliases (`auto`, `fast`,
  `coder`, `deep`, `embedding`); (2) backend adapters beyond llama.cpp
  (Ollama, generic OpenAI, vLLM; AirLLM experimental, off by default);
  (3) app profiles (`apps.yaml`: per-client default model, allowed models,
  rate limits, privacy); (4) standalone dashboard; (5) MCP server + A2A agent
  card. Keep each phase a separate branch and PR.
- Renaming modules toward a layered layout (`api/`, `routing/`, `backends/`,
  `telemetry/`) is allowed once tests cover the seam being moved — never as a
  big-bang rewrite.

## Verification

- `python -m pytest tests/ -q` must pass with no fleet running.
- `python -m py_compile` touched files.
- For provider changes, run a `/health` + `/v1/chat/completions` smoke against
  a live fleet when available (`scripts/smoke_provider.*`).

## Child DOX Index

- `local_model_router/service/AGENTS.md` — HTTP surface, auth, forwarding.
- `local_model_router/helpers/AGENTS.md` — orchestration and routing helpers.
- `tests/AGENTS.md` — test conventions (hermetic, no live fleet).

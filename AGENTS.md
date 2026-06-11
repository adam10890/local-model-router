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
- `local_model_router/routing/` — model aliases and routing policy.
- `local_model_router/upstreams/` — upstream backend adapters
  (`openai_compatible` covers Ollama/vLLM/LocalAI/LM Studio; `airllm` is
  recognized but experimental and non-serving). Keys via env only.
- `local_model_router/apps/` — app/client profiles (`conf/apps.yaml`):
  default model, allowed models, auto-route policy per `X-App-Id`.
- `conf/` — fleet description. `llama_cpp_servers.yaml` is local-only
  (gitignored); the `.example` variant is the committed reference.
  `upstreams.yaml` and `apps.yaml` are committed defaults (no secrets).
- `tests/` — hermetic pytest suite; no live fleet or GPU required.
- The service is lifecycle-free **by default**: it routes to running slots.
  Fleet control (start/stop slots via `BackendManager` — docker, subprocess,
  or remote per `global.backend`) is opt-in behind
  `A0_LMM_ROUTER_ENABLE_FLEET_CONTROL=1`; the docker backend additionally
  requires the `[docker]` extra. Without the flag, no Docker socket access.

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
- Phase roadmap: (1) ✅ `GET /v1/models` + model aliases; (2) ✅ upstream
  adapters (`conf/upstreams.yaml`, `<name>/<model>` namespacing); (3) ✅ app
  profiles (`conf/apps.yaml`); (4) ✅ standalone dashboard at `/ui`;
  (5) ✅ MCP server (`local_model_router/mcp`, `[mcp]` extra) + A2A agent
  card (`/.well-known/agent-card.json`, `POST /a2a`); (6) ✅ opt-in fleet
  lifecycle control (`/fleet/start|stop`, `/fleet/slots/{id}/start|stop`,
  dashboard buttons; gated by `A0_LMM_ROUTER_ENABLE_FLEET_CONTROL=1`).
  Future: per-app API keys, rate limits, `/v1/embeddings` passthrough,
  `/metrics`, `/routing/history`, AirLLM serving adapter, upstream-aware
  auto-routing, Cookbook/Compare dashboard pages, integration docs set.
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

Smaller areas without child contracts yet: `routing/` (aliases),
`upstreams/`, `apps/`, `mcp/` (ported from the plugin; mutating tools gated
by `MCP_ALLOW_MUTATING_TOOLS`), `a2a/` (card + skills; card is public, skills
honor the API key), `dashboard/` (single static page at `/ui`; data calls
carry the user-entered key).

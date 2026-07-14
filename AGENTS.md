# DOX contract — local-model-router (repo root)

## Purpose

Imperium is a standalone local-first model router. One OpenAI-compatible gateway in front
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
- `local_model_router/upstreams/` — OpenAI-compatible upstream adapters for
  Ollama/vLLM/LocalAI/LM Studio. Keys via env only.
- `local_model_router/apps/` — app/client profiles (`conf/apps.yaml`):
  default model, allowed models, auto-route policy per `X-App-Id`.
- `local_model_router/harnesses/` — dedicated harness identities, pinned model
  connections, setup manifests, and atomic `conf/harnesses.yaml` persistence.
- `conf/` — fleet description. `llama_cpp_servers.yaml` is local-only
  (gitignored); the `.example` variant is the committed reference.
  `upstreams.yaml`, `apps.yaml`, and `harnesses.yaml` are committed defaults
  (no secrets).
- `docs/` — durable runbooks and development workflow docs.
- `CONTRIBUTING.md` — public Git and contribution workflow; keep it aligned
  with `docs/development/git-workflow.md`.
- `CLAUDE.md` — Claude Code entrypoint; it points back to this DOX contract.
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
- Dedicated harness paths are authoritative: one pinned model per connection,
  with Agent Zero's named chat/utility connections as the explicit exception.
- Config previews and telemetry must redact secrets and never include prompt
  bodies.

## Work Guidance

- Keep increments small and behavior-preserving; this codebase is trusted by
  a live setup.
- Default to Ponytail-style implementation: stdlib/native first, shortest
  working diff, no speculative abstractions.
- Git workflow: keep `main` shippable; use `dev/<slug>` for active work,
  `ready/<slug>` for verified merge candidates, and delete merged branches
  locally and remotely. Detailed rules live in
  `docs/development/git-workflow.md`.
- Agent handoffs between Codex, Claude Code, or humans must name the current
  branch, changed files, verification commands, and unresolved risks.
- Phase roadmap: (1) ✅ `GET /v1/models` + model aliases; (2) ✅ upstream
  adapters (`conf/upstreams.yaml`, `<name>/<model>` namespacing); (3) ✅ app
  profiles (`conf/apps.yaml`); (4) ✅ standalone dashboard at `/ui`;
  (5) ✅ MCP server (`local_model_router/mcp`, `[mcp]` extra) + A2A agent
  card (`/.well-known/agent-card.json`, `POST /a2a`); (6) ✅ opt-in fleet
  lifecycle control (`/fleet/start|stop`, `/fleet/slots/{id}/start|stop`,
  dashboard buttons; gated by `A0_LMM_ROUTER_ENABLE_FLEET_CONTROL=1`);
  (7) ✅ dashboard v2: tabs (Overview / Harnesses / Cookbook) +
  `GET /cookbook` (`local_model_router/cookbook/` — GGUF header parsing,
  VRAM fit math, per-role recommendations); (8) ready candidate on
  `ready/orca-inspired-routing`: Local-first+ catalog, capability-aware `auto`,
  routing strategies, analytics, MCP discovery, Compare / Routing dashboard,
  and integration snippets inspired by OrcaRouter patterns without vendor
  code or cloud BYOK scope.
  (9) ✅ router-backed `POST /v1/embeddings` and HTTP-only MCP bridge.
  (10) ✅ ranked-candidate failover chains, health-probe TTL cache
  (`global.health_cache_ttl` / `A0_LMM_ROUTER_HEALTH_CACHE_TTL`), and opt-in
  upstream-aware auto-routing (`A0_LMM_ROUTER_AUTO_UPSTREAMS=1` + declared
  `models:` per upstream; local-first preserved, embeddings stay local).
  (11) ✅ router-backed built-in agent library (`GET /agents`,
  `POST /agents/{id}/runs`, `[agents]` extra); agent prompts remain out of
  telemetry and per-agent `local_only` preserves fleet-only handling.
  Future: per-app API keys, rate limits, `/metrics`, `/routing/history`,
  one-click "apply recommendation" (cookbook → fleet control).
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
- `docs/AGENTS.md` — project documentation and development workflow docs.
- `tests/AGENTS.md` — test conventions (hermetic, no live fleet).

- `local_model_router/cookbook/AGENTS.md` — GGUF parsing + fit math +
  recommendations.

Smaller areas without child contracts yet: `routing/` (aliases),
  `upstreams/`, `apps/`, `harnesses/`, `mcp/` (ported from the plugin; mutating tools gated
by `MCP_ALLOW_MUTATING_TOOLS`), `a2a/` (card + skills; card is public, skills
honor the API key), `dashboard/` (single static page at `/ui`; data calls
carry the user-entered key).

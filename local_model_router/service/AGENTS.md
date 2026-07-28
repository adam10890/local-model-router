# DOX contract — local_model_router/service

## Purpose

The HTTP surface: Starlette app (`app.py`), dry-run routing intent
(`routing_intent.py`), read-only fleet observer (`observer.py`), Fleet
Manager control plane (`fleet_manager.py`), and opt-in fleet lifecycle
control (`fleet_control.py`). Agent ticket coordination lives in
`agent_orchestrator.py`. Entry point: `__main__.py`
(`python -m local_model_router`).

## Ownership

- `app.py` owns routes, auth middleware, and chat-completions forwarding.
- `routing_intent.py` owns the Agent Client Contract schema and policy.
- `observer.py` owns read-only fleet views; it never mutates state.
- `fleet_manager.py` owns agent identity, bounded admission, SQLite state.
- `fleet_control.py` owns the opt-in start/stop facade over
  `helpers/llama_cpp_manager.BackendManager`.
- `readiness.py` turns setup, runtime, model, and fleet state into the stable
  Simple-dashboard status and next-action contract.
- `agent_library.py` owns the safe built-in agent catalog and self-routed
  Pydantic AI runner.
- `agent_orchestrator.py` owns observe-first plan/ticket state, workspace
  packets, DOX chain snapshots, and wake markers. It must not launch
  containers or edit `AGENTS.md` files directly.

## Local Contracts

- `POST /routing/request` is dry-run intent routing.
- `GET /agents` exposes only public metadata for the built-in catalog;
  `POST /agents/{id}/runs` calls this service's `/v1/chat/completions` endpoint
  asynchronously. It must pass configured role/task/local-only intent through
  the normal routing path and must never expose or telemetry-store prompts.
- `GET /routing/models`, `GET /routing/models/{id}`,
  `GET /routing/evaluations`, and `GET /routing/analytics` are safe
  discovery/telemetry surfaces. They must never return API keys, prompt
  bodies, responses, or raw request content.
- `/orchestrator/*` is a protected coordination surface for multi-agent
  plans and sub-agent instance heartbeats. It stores prompts in ticket
  workspaces, not fleet telemetry; list/summary endpoints must not expose
  prompt bodies. Persona prompts live as workspace files referenced by
  `persona_prompt_path`, not as list payload text. Sub agents submit DOX
  reports or explicit unchanged reasons. Ticket claims are atomic, leases are
  renewable by the owner and reclaimable after expiry, and progress logs are
  append-only. Complete/block actions require the active owner. The surface is
  retained for compatibility, hidden from the dashboard, and returns
  `Deprecation: true`.
- `/setup/*` is loopback-only, protected by an ephemeral setup token, and may
  download or write only after explicit reviewed consent. `GET /ui/status`
  uses `configuration`/`system` issue categories and stable issue/action codes
  instead of asking the UI to infer remedies.
- OpenAI-compatible endpoints reuse the routing decision path and forward to
  the selected llama.cpp slot; no duplicate routing policy.
- `POST /v1/embeddings` selects the local `embed` lane through that same
  routing decision path; MCP must call this endpoint rather than a slot directly.
- `/harnesses/{id}/v1` and named-connection variants pin one configured model.
  Client model/role hints cannot escape the path. Connection/timeout/unloaded
  targets return `503 harness_model_unavailable` without cross-model failover.
  Upstream capability gaps (for example missing mmproj / unsupported image
  input) return `upstream_capability_missing` with a sanitized upstream
  message. `GET .../models` marks the connection `connected` and publishes
  effective capabilities of the pinned target; a successful chat marks
  `verified`. Hermes setup manifests always emit explicit
  `supports_vision: true|false`.
- `POST /harnesses` requires both bearer auth and
  `A0_LMM_ROUTER_ENABLE_CONFIG_WRITES=1`; writes are atomic and backed up.
- `model=auto` is capability-aware: tools, vision payloads, JSON mode,
  estimated tokens, app profile, strategy, health, latency hints, quality
  hints, and local resource cost hints may affect ranking. Preserve
  explainability with reason codes and score inputs.
- Model name contract: recognized aliases (`auto`, `fast`, `coder`, … —
  defined in `local_model_router/routing/aliases.py`) forward the routing
  decision's model; unrecognized names are explicit model requests and pass
  through to the upstream verbatim. Do not silently rewrite explicit ids.
- `<upstream>/<model>` names (e.g. `ollama/llama3.3:70b`) bypass fleet
  routing and forward straight to that upstream with its env-sourced auth.
  They never consume the local lane. A bounded upstream uses its independent
  `upstream:<name>` lane; an unbounded upstream delegates admission to the
  provider.
- Chat lifecycle is `Normalize -> Resolve Target -> Admission -> Forward ->
  Finalize`. New request records finish once as `completed`, `failed`, or
  `rejected`, and only the lane actually acquired may be released. Streaming
  holds admission until completion, upstream failure, or client disconnect.
- Every chat response after request allocation carries `X-A0-Request-ID` and
  `X-A0-Admission-Lane`. A full lane returns 429 with its limits. Pinned
  harness failures remain 503 and never trigger cross-model fallback.
- Model evaluations are explicit CLI runs stored in the existing snapshot
  mechanism. The latest safe snapshot may supply quality, latency, resource,
  and reliability inputs to the existing ranker; do not add a second scoring
  formula or automatically repin harnesses.
- App profile enforcement runs before alias resolution: empty model takes
  the profile default; `auto` is allowed whenever `allow_auto_route` is true;
  disallowed explicit models return 403 with a policy code.
- `GET /v1/models` lists aliases first; live slot models merge into matching
  alias entries as `meta.live` instead of duplicating ids. Capability/context
  metadata is additive and must remain safe for OpenAI-compatible clients to
  ignore.
- Prompt cache is opt-in via `A0_LMM_ROUTER_PROMPT_CACHE=1`, in-memory only,
  and limited to deterministic non-streaming requests. Cache status is exposed
  in response headers and telemetry, but prompt bodies are not stored.
- Lifecycle is opt-in: `/fleet/start`, `/fleet/stop`, and
  `/fleet/slots/{id}/start|stop` return 403 `fleet_control_disabled` unless
  `A0_LMM_ROUTER_ENABLE_FLEET_CONTROL=1` is set at startup. With the flag on,
  what "start" does follows `global.backend` in the fleet config (`docker` /
  `subprocess` / `remote`). The docker backend needs the `[docker]` extra.
  These endpoints always honor the bearer API key like everything else.
- Default posture stays: no Docker socket, no container lifecycle.
  No prompt logging ever.
- Public binds without auth must keep refusing to start.

## Work Guidance

- Keep forwarding, auth, and packaging as separate implementation gates.
- New endpoints get a route in `app.py`, schema in `routing_intent.py` (or a
  new module), and tests in `tests/`.

## Verification

- `python -m pytest tests/test_routing_intent.py tests/test_observer_service.py tests/test_openai_chat_completions.py tests/test_fleet_manager.py tests/test_fleet_control.py tests/test_local_first_plus_api.py tests/test_agent_orchestrator.py -q`

## Child DOX Index

No child AGENTS.md files yet.

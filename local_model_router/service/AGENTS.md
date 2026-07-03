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
- `agent_orchestrator.py` owns observe-first plan/ticket state, workspace
  packets, DOX chain snapshots, and wake markers. It must not launch
  containers or edit `AGENTS.md` files directly.

## Local Contracts

- `POST /routing/request` is dry-run intent routing.
- `GET /routing/models`, `GET /routing/models/{id}`, and
  `GET /routing/analytics` are safe discovery/telemetry surfaces. They must
  never return API keys, prompt bodies, or raw request content.
- `/orchestrator/*` is a protected coordination surface for multi-agent
  plans and sub-agent instance heartbeats. It stores prompts in ticket
  workspaces, not fleet telemetry; list/summary endpoints must not expose
  prompt bodies. Persona prompts live as workspace files referenced by
  `persona_prompt_path`, not as list payload text. Sub agents submit DOX
  reports or explicit unchanged reasons.
- OpenAI-compatible endpoints reuse the routing decision path and forward to
  the selected llama.cpp slot; no duplicate routing policy.
- `/harnesses/{id}/v1` and named-connection variants pin one configured model.
  Client model/role hints cannot escape the path, and an unavailable target
  returns `503 harness_model_unavailable` without cross-model failover.
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
  Upstream requests do not consume the fleet queue (it guards local VRAM).
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

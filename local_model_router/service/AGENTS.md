# DOX contract — local_model_router/service

## Purpose

The HTTP surface: Starlette app (`app.py`), dry-run routing intent
(`routing_intent.py`), read-only fleet observer (`observer.py`), and Fleet
Manager control plane (`fleet_manager.py`). Entry point: `__main__.py`
(`python -m local_model_router`).

## Ownership

- `app.py` owns routes, auth middleware, and chat-completions forwarding.
- `routing_intent.py` owns the Agent Client Contract schema and policy.
- `observer.py` owns read-only fleet views; it never mutates state.
- `fleet_manager.py` owns agent identity, bounded admission, SQLite state.

## Local Contracts

- `POST /routing/request` is dry-run intent routing.
- OpenAI-compatible endpoints reuse the routing decision path and forward to
  the selected llama.cpp slot; no duplicate routing policy.
- Model name contract: recognized aliases (`auto`, `fast`, `coder`, … —
  defined in `local_model_router/routing/aliases.py`) forward the routing
  decision's model; unrecognized names are explicit model requests and pass
  through to the upstream verbatim. Do not silently rewrite explicit ids.
- `GET /v1/models` lists aliases first; live slot models merge into matching
  alias entries as `meta.live` instead of duplicating ids.
- No Docker socket. No container lifecycle. No prompt logging.
- Public binds without auth must keep refusing to start.

## Work Guidance

- Keep forwarding, auth, and packaging as separate implementation gates.
- New endpoints get a route in `app.py`, schema in `routing_intent.py` (or a
  new module), and tests in `tests/`.

## Verification

- `python -m pytest tests/test_routing_intent.py tests/test_observer_service.py tests/test_openai_chat_completions.py tests/test_fleet_manager.py -q`

## Child DOX Index

No child AGENTS.md files yet.

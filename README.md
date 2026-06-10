# local-model-router

**A local-first model router: one OpenAI-compatible gateway in front of your
local llama.cpp fleet — with explainable routing, health/failover, and a
fleet-manager control plane.**

Point every local AI tool (Agent Zero, Hermes, n8n, Open WebUI, Aider, custom
scripts, …) at a single base URL instead of wiring each one to a model server:

```text
http://127.0.0.1:9000/v1
```

## Why

Local AI setups accumulate tools, and every tool wants its own model wiring.
Swapping a model means touching N configs; a dying server means N broken
tools. A router inverts that: tools talk to one stable endpoint, the router
decides which local model serves each request, explains the decision, and
fails over when slots get unhealthy.

This project was extracted from
[`a0_lmm_router`](https://github.com/adam10890/a0_lmm_router), an Agent Zero
plugin that grew a standalone provider. Here the router is the product;
Agent Zero is client #1, not the owner.

## Status

**0.1.0 — working extraction.** What works today:

| Surface | Endpoint | Notes |
|---|---|---|
| Health | `GET /health` | open, no auth |
| Slots | `GET /slots`, `GET /health/slots` | fleet view + live probes |
| Routing (dry-run) | `POST /routing/request` | explainable intent routing |
| Routing preview | `GET /routing/preview` | which slot a role would get |
| OpenAI-compatible | `POST /v1/chat/completions` | streaming + non-streaming forwarding to the selected slot |
| Fleet Manager | `GET /fleet/status`, `GET /fleet/agents`, `POST /fleet/agents/register` | agent identity, bounded queueing, SQLite telemetry |
| Config preview | `GET /config/preview` | secrets redacted |

Roadmap (see `AGENTS.md`): `GET /v1/models`, model aliases (`auto`, `fast`,
`coder`, …), multi-backend adapters (Ollama, generic OpenAI, vLLM, AirLLM
experimental), app profiles, standalone dashboard, MCP server, A2A agent card.

## Quickstart

Requires Python 3.10+ and a running llama.cpp fleet (Router Mode or
multi-slot) — the router routes to it, it does not start it.

```powershell
# 1. install
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"

# 2. configure — describe your fleet
Copy-Item conf\llama_cpp_servers.example.yaml conf\llama_cpp_servers.yaml
# edit conf\llama_cpp_servers.yaml (slots, hosts, ports)

# 3. run
.venv\Scripts\python -m local_model_router
# → http://127.0.0.1:9000  (OBSERVER_HOST / OBSERVER_PORT to change)
```

Or use the wrapper scripts: `scripts\run_provider.ps1` (Windows),
`scripts/run_provider.sh` (WSL/Linux), plus `smoke_provider.*` for a
post-start check.

## Calling it from the OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:9000/v1", api_key="local")

resp = client.chat.completions.create(
    model="chat",  # router alias; "auto" routing lands in a later phase
    messages=[{"role": "user", "content": "Write a short Python function."}],
)
print(resp.choices[0].message.content)
```

## Security

- Binds to `127.0.0.1` by default.
- Set `A0_LMM_ROUTER_API_KEY` to require `Authorization: Bearer <key>` on
  everything except `/health`.
- Public binds (anything not loopback) **refuse to start** without an API key
  unless you explicitly set `A0_LMM_ROUTER_ALLOW_PUBLIC_NO_AUTH=1`.
- Config previews redact secrets. Prompts are not logged.

See `.env.example` for the full environment surface.

## Connecting Agent Zero

Agent Zero ≥ the quiet-mode plugin (a0_lmm_router v1.4) talks to the fleet
directly as an OpenAI-compatible provider. Point the `lmm_router` provider's
API base at this router instead of a raw llama.cpp slot to gain failover,
routing telemetry, and the fleet-manager queue:

```text
api_base: http://host.docker.internal:9000/v1
```

## Development

```powershell
.venv\Scripts\python -m pytest tests/ -q     # full suite
```

This repo follows the [DOX](https://github.com/agent0ai/dox) living-docs
convention: every significant directory has an `AGENTS.md` contract. Read the
local contract before editing an area; update it when behavior changes.

## License

MIT

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
| OpenAI-compatible | `GET /v1/models`, `POST /v1/chat/completions` | aliases + live models; streaming + non-streaming forwarding |
| Fleet Manager | `GET /fleet/status`, `GET /fleet/agents`, `POST /fleet/agents/register` | agent identity, bounded queueing, SQLite telemetry |
| Backends | `GET /backends` | local fleet + configured upstreams with capabilities |
| App profiles | `GET /apps` | per-client routing policy |
| Config preview | `GET /config/preview` | secrets redacted |
| Dashboard | `GET /ui` | standalone Alpine.js panel: slots, backends, models, queue, routing test |
| A2A | `GET /.well-known/agent-card.json`, `POST /a2a` | agent card + skills for agent-to-agent use |
| MCP | `python -m local_model_router mcp` | Streamable HTTP MCP server (port 8095, `[mcp]` extra) |

**Three surfaces, one rule:** the OpenAI-compatible API is for model clients;
MCP is for tool-using agents; A2A is for agent-to-agent collaboration. All
three reuse the same routing engine — no duplicate policy.

**Model names:** send an alias — `auto` (routes by task type), `chat`/`deep`,
`fast`/`utility`, `coder`, `embedding`, `scribe` — and the router picks the
slot and model. Send any other model id and it passes through verbatim to the
selected slot (Router Mode fleets can hot-swap to it). Send
`<upstream>/<model>` (e.g. `ollama/llama3.3:70b`) to target an upstream
backend configured in `conf/upstreams.yaml` — one `openai_compatible`
adapter covers Ollama, vLLM, LocalAI, and LM Studio. AirLLM is recognized as
an experimental, non-serving entry.

**App profiles:** identify your client with the `X-App-Id` header and
`conf/apps.yaml` controls its default model and allowed models. Unknown apps
get the permissive default profile.

**CLI:** `python -m local_model_router [serve|doctor|list-models|test-route|config-check]`

Roadmap (see `AGENTS.md`): multi-backend adapters (Ollama, generic OpenAI,
vLLM, AirLLM experimental), app profiles, standalone dashboard, MCP server,
A2A agent card.

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
    model="auto",  # or: chat, deep, fast, coder, embedding — or an exact model id
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

## Docker

```bash
echo "A0_LMM_ROUTER_API_KEY=change-me" > .env
docker compose up -d
curl http://localhost:9000/health
```

The container binds to localhost only and refuses to start without an API
key. The fleet config is mounted read-only from `./conf`;
`host.docker.internal` reaches host-published llama.cpp ports.

## MCP server

```powershell
.venv\Scripts\pip install -e ".[mcp]"
.venv\Scripts\python -m local_model_router mcp     # Streamable HTTP on :8095/mcp
```

Tools: `chat_completion`, `utility_completion`, `route_completion`,
`get_embeddings`, `fleet_status`, `list_slots` — plus admin tools
(`start_fleet`, `start_slot`, `stop_slot`) **only** when
`MCP_ALLOW_MUTATING_TOOLS=1`. Bearer auth is on by default; inspect with
`npx @modelcontextprotocol/inspector http://127.0.0.1:8095/mcp`.

## Development

```powershell
.venv\Scripts\python -m pytest tests/ -q     # full suite
```

This repo follows the [DOX](https://github.com/agent0ai/dox) living-docs
convention: every significant directory has an `AGENTS.md` contract. Read the
local contract before editing an area; update it when behavior changes.

## License

MIT

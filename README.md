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

**0.3.0-dev - Local-first+ routing catalog.** What works today:

| Surface | Endpoint | Notes |
|---|---|---|
| Health | `GET /health` | open, no auth |
| Slots | `GET /slots`, `GET /health/slots` | fleet view + live probes |
| Routing (dry-run) | `POST /routing/request` | explainable capability-aware intent routing |
| Routing preview | `GET /routing/preview` | which slot a role would get |
| Routing catalog | `GET /routing/models`, `GET /routing/models/{id}`, `GET /routing/analytics` | safe model cards, recent decisions, latency/fallback/cache stats |
| Agent orchestration | `POST /orchestrator/plans`, `GET /orchestrator/plans`, `GET /orchestrator/summary`, `GET /orchestrator/instances`, `POST /orchestrator/instances/{id}`, `POST /orchestrator/tickets/{id}/submit` | observe-first plan/ticket packets, sub-agent instance heartbeats, DOX reports, artifacts, wake markers |
| OpenAI-compatible | `GET /v1/models`, `POST /v1/chat/completions` | aliases + live/upstream models; capabilities metadata; streaming + non-streaming forwarding |
| Fleet Manager | `GET /fleet/status`, `GET /fleet/agents`, `POST /fleet/agents/register` | agent identity, bounded queueing, SQLite telemetry |
| Fleet control (opt-in) | `POST /fleet/start`, `POST /fleet/stop`, `POST /fleet/slots/{id}/start` + `/stop` | start/stop slots; off unless `A0_LMM_ROUTER_ENABLE_FLEET_CONTROL=1` |
| Backends | `GET /backends` | local fleet + configured upstreams with capabilities |
| App profiles | `GET /apps` | per-client routing policy |
| Config preview | `GET /config/preview` | secrets redacted |
| Dashboard | `GET /ui` | Overview, Connect an agent, Compare / Routing, Orchestration, Cookbook |
| Cookbook | `GET /cookbook` | scans your GGUF folder, VRAM fit math, per-role model recommendations with confidence grades |
| A2A | `GET /.well-known/agent-card.json`, `POST /a2a` | agent card + skills for agent-to-agent use |
| MCP | `python -m local_model_router mcp` | Streamable HTTP MCP server (port 8095, `[mcp]` extra) |

**Three surfaces, one rule:** the OpenAI-compatible API is for model clients;
MCP is for tool-using agents; A2A is for agent-to-agent collaboration. All
three reuse the same routing engine — no duplicate policy.

**Model names:** send an alias - `auto` (capability-aware routing),
`chat`/`deep`, `fast`/`utility`, `coder`, `embedding`, `scribe` - and the
router picks the slot and model. Send any other model id and it passes through
verbatim to the selected slot (Router Mode fleets can hot-swap to it). Send
`<upstream>/<model>` (e.g. `ollama/llama3.3:70b`) to target an upstream
backend configured in `conf/upstreams.yaml` — one `openai_compatible`
adapter covers Ollama, vLLM, LocalAI, and LM Studio. AirLLM is recognized as
an experimental, non-serving entry.

**Routing strategies:** `auto` uses the local catalog to rank slots by
capabilities (`tools`, vision payloads, JSON mode, context size), health,
latency hints, quality hints, resource cost hints, app profile, and privacy
flags. Supported strategies are `balanced_local` (default), `fastest`,
`quality`, and `economy`. Decisions expose reason codes, score inputs, and
response headers for requested model, resolved model, selected slot, selected
strategy, and cache status.

**App profiles:** identify your client with the `X-App-Id` header and
`conf/apps.yaml` controls its default model and allowed models. Unknown apps
get the permissive default profile.

**Prompt cache:** disabled by default. Set `A0_LMM_ROUTER_PROMPT_CACHE=1` to
enable an in-memory deterministic cache for non-streaming requests with
`temperature=0` or a fixed `seed`. Cache data is process-local and prompt
bodies are never written to telemetry.

**Agent orchestration:** V1 is coordination only. It writes plan/ticket
workspaces under `A0_AGENT_ORCH_DIR` (or temp by default), creates draft
`compose.plan.yaml` files, snapshots relevant DOX chains, and writes
`WAKE.json` when the planner should resume. Sub-agent runners can report
instance status/heartbeats for the dashboard and Agent Zero through
`POST /orchestrator/instances/{id}`. Tickets and instances may carry
persona metadata (`persona_id`, `persona_name`, `persona_prompt_path`) so a
runner can prepend a fixed role prompt before task-specific instructions. It
does not run Docker.

**CLI:** `python -m local_model_router [serve|doctor|list-models|test-route|config-check]`

Roadmap (see `AGENTS.md`): per-app API keys, rate limits, `/v1/embeddings`
passthrough, Prometheus-style `/metrics`, upstream-aware auto-routing, and
one-click cookbook recommendations.

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

## Fleet control (optional)

By default the router only **routes** — your llama.cpp fleet is started by
you (compose files, scripts). Set this in `.env` to let the router also
start and stop slots, from the API or the dashboard buttons:

```text
A0_LMM_ROUTER_ENABLE_FLEET_CONTROL=1
```

What "start" does follows `global.backend` in `conf/llama_cpp_servers.yaml`:

- `docker` — the router starts real llama.cpp containers, rendering the full
  flag set from the slot config (GPU layers, context, flash attention,
  router mode, MTP). Requires `pip install -e ".[docker]"` and a running
  Docker daemon.
- `subprocess` — spawns local `llama-server` processes.
- `remote` — registers and health-checks servers you started yourself
  (no process control).

The endpoints honor the API key like everything else, and the dashboard
shows start/stop buttons per slot only when the flag is on.

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
`get_embeddings`, `fleet_status`, `list_slots`, `list_models`,
`model_card`, `providers_list`, `route_preview` - plus admin tools
(`start_fleet`, `start_slot`, `stop_slot`) **only** when
`MCP_ALLOW_MUTATING_TOOLS=1`. Bearer auth is on by default; inspect with
`npx @modelcontextprotocol/inspector http://127.0.0.1:8095/mcp`.

See `docs/INTEGRATIONS.md` for Claude Code MCP, Open WebUI, Dify, Aider, and
Vercel AI SDK style setup snippets.

## Development

```powershell
.venv\Scripts\python -m pytest tests/ -q     # full suite
```

See `CONTRIBUTING.md` and `docs/development/git-workflow.md` for branch
naming, merge, cleanup, and agent handoff rules.

This repo follows the [DOX](https://github.com/agent0ai/dox) living-docs
convention: every significant directory has an `AGENTS.md` contract. Read the
local contract before editing an area; update it when behavior changes.

## License

MIT

# Imperium (local-model-router)

**A local-first model router: one OpenAI-compatible gateway in front of your
local llama.cpp fleet — with explainable routing, health/failover, and a
fleet-manager control plane.**

Give every local AI harness a stable router URL pinned to its compute model:

```text
http://127.0.0.1:9000/harnesses/hermes/v1
http://127.0.0.1:9000/harnesses/pi/v1
```

The generic `http://127.0.0.1:9000/v1` surface remains available for clients
that intentionally want aliases and automatic routing.

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

**0.8.0 - Guided local AI from first launch.** What works today:

| Surface | Endpoint | Notes |
|---|---|---|
| Health | `GET /health` | open, no auth |
| Slots | `GET /slots`, `GET /health/slots` | fleet view + live probes |
| Routing (dry-run) | `POST /routing/request` | explainable capability-aware intent routing |
| Routing preview | `GET /routing/preview` | which slot a role would get |
| Routing catalog | `GET /routing/models`, `GET /routing/models/{id}`, `GET /routing/analytics` | safe model cards, recent decisions, latency/fallback/cache stats |
| Legacy orchestration | protected `/orchestrator/*` | deprecated compatibility API; hidden from the dashboard, with no removal date assigned |
| Agent library | `GET /agents`, `POST /agents/{id}/runs` | four built-in prompt-backed agents through router-selected models; runner included in Windows bundles |
| OpenAI-compatible | `GET /v1/models`, `POST /v1/chat/completions`, `POST /v1/embeddings` | aliases + live/upstream models; chat streaming and local embedding forwarding |
| Harnesses | `GET /harnesses`, `GET /harnesses/{id}`, dedicated `.../v1/models` + `.../v1/chat/completions` | one authoritative model per connection; Agent Zero has chat + utility |
| Fleet Manager | `GET /fleet/status`, `GET /fleet/agents`, `POST /fleet/agents/register` | agent identity, bounded queueing, SQLite + cached GPU/CPU/RAM telemetry |
| Fleet control (opt-in) | `POST /fleet/start`, `POST /fleet/stop`, `POST /fleet/slots/{id}/start` + `/stop` | start/stop slots; off unless `A0_LMM_ROUTER_ENABLE_FLEET_CONTROL=1` |
| Backends | `GET /backends` | local fleet + configured upstreams with capabilities |
| Compute budget | `GET /compute/budget` | local hardware headroom + per-provider usage vs declared/live limits |
| App profiles | `GET /apps` | per-client routing policy |
| Config preview | `GET /config/preview` | secrets redacted |
| Simple dashboard | `GET /ui` | Home, Chat, Models, and Connections; light/dark, English/Hebrew, LTR/RTL |
| Advanced dashboard | `GET /ui#/advanced/fleet` | Fleet, Routing, Configuration, and Diagnostics |
| Readiness | `GET /ui/status` | stable blocking/optional issues and one recommended next action |
| First-run setup | loopback-only `/setup/*` | hardware discovery, reviewed plan, managed downloads, progress, cancellation, and smoke test |
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
adapter covers Ollama, vLLM, LocalAI, and LM Studio.

**Routing strategies:** `auto` uses the local catalog to rank slots by
capabilities (`tools`, vision payloads, JSON mode, context size), health,
latency hints, quality hints, resource cost hints, app profile, and privacy
flags. Supported strategies are `balanced_local` (default), `fastest`,
`quality`, and `economy`. Decisions expose reason codes, score inputs, and
response headers for requested model, resolved model, selected slot, selected
strategy, and cache status.

**App profiles:** identify your client with the `X-App-Id` header and
`conf/apps.yaml` controls its default model and allowed models. Unknown apps
get the permissive default profile. Dedicated harness connections instead use
`conf/harnesses.yaml`; their URL overrides client model and role hints.

**Prompt cache:** disabled by default. Set `A0_LMM_ROUTER_PROMPT_CACHE=1` to
enable an in-memory deterministic cache for non-streaming requests with
`temperature=0` or a fixed `seed`. Cache data is process-local and prompt
bodies are never written to telemetry.

**Legacy orchestration:** V1 coordination remains available for compatibility
and returns `Deprecation: true`, but it is no longer shown in the dashboard.
It writes plan/ticket workspaces under `A0_AGENT_ORCH_DIR` (or temp by default), creates draft
`compose.plan.yaml` files, snapshots relevant DOX chains, and writes
`WAKE.json` when the planner should resume. Sub-agent runners can report
instance status/heartbeats for the dashboard and Agent Zero through
`POST /orchestrator/instances/{id}`. Tickets and instances may carry
persona metadata (`persona_id`, `persona_name`, `persona_prompt_path`) so a
runner can prepend a fixed role prompt before task-specific instructions. It
does not run Docker. See `docs/future-orchestration.md` for the replacement
gates; the `/agents` library below is separate and is not deprecated.

## Built-in agent library

Source installations can add the optional runner and point its self-call at
this router. Self-contained Windows bundles already include it:

```powershell
.venv\Scripts\pip install -e ".[agents]"
$env:A0_LMM_ROUTER_AGENT_BASE_URL = "http://127.0.0.1:9000/v1"
```

On Windows, `SETUP.bat` installs the `[agents]` extra and `START.bat` derives
that self-call URL from `OBSERVER_HOST` and `OBSERVER_PORT` when it is not
already set in `.env`.

List the built-in agents and run one with input supplied in the request:

```powershell
curl http://127.0.0.1:9000/agents
curl -X POST http://127.0.0.1:9000/agents/code-review/runs `
  -H "Content-Type: application/json" `
  -d '{"input":"Review this diff: ..."}'
```

The catalog supplies each agent's role, task type, routing strategy, and
optional `local_only` policy. Calls carry `X-App-Id: agent_library` and the
agent id through normal routing analytics. With
`A0_LMM_ROUTER_AUTO_UPSTREAMS=1`, agents without `local_only` may use a
declared upstream only after local routing is exhausted; local-only agents
never leave the fleet. Input is limited to 64 KiB and a run times out after
120 seconds. Prompts are not exposed by the catalog or stored in telemetry.

**CLI:** `imperium [serve|setup|doctor|list-models|test-route|config-check|update|rollback]`

Roadmap (see `AGENTS.md`): first-run stabilization, per-app API keys, rate
limits, Prometheus-style `/metrics`, and one-click cookbook recommendations.

## Windows first run

Download the Windows release ZIP, extract it, and run
`Install-Imperium.bat`. The bundle supplies a private Python runtime and opens
a six-stage browser wizard. Native llama.cpp is recommended; Docker is
optional and is never started automatically. Every download shows its source,
license, size, destination, estimated fit, and configuration preview before
consent. Qwen3 1.7B Q8 is the first-run default with a conservative managed
4K context. If current free memory is too low, setup asks the user to close
other model servers or applications and scan again before it starts llama.cpp.

The application installs under `%LOCALAPPDATA%\Programs\Imperium`. Models,
state, backups, and managed configuration live under
`%LOCALAPPDATA%\Imperium`. The previous application version is retained for
rollback. Run `Rollback-Imperium.bat` from an extracted release or installed
folder to atomically swap the active and previous application versions.

Useful recovery commands:

```powershell
imperium setup --status
imperium setup --repair
imperium doctor --json
imperium update --check
imperium update --yes
imperium rollback               # managed llama.cpp runtime
.\Rollback-Imperium.bat          # Imperium application
```

## Developer quickstart

Source development requires Python 3.10+. Existing repository configuration
is preserved; otherwise `START.bat` opens the first-run wizard.

```powershell
# 1. install
python -m venv .venv
.venv\Scripts\pip install -e ".[dev,mcp,agents]"

# 2. start or continue setup
.venv\Scripts\imperium setup
# → http://127.0.0.1:9000/ui#/setup
```

Or use the wrapper scripts: `scripts\run_provider.ps1` (Windows),
`scripts/run_provider.sh` (WSL/Linux), plus `smoke_provider.*` for a
post-start check and `scripts/smoke_harnesses.py` to verify every configured
dedicated harness connection.

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

## Compute Budget

One place tracks all available compute — the local llama.cpp fleet plus
configured subscription/upstream providers — and routing stays aware of it.
Declare rolling-window `limits` (`"5h"`/`"7d"`, `max_tokens`/`max_requests`)
on an upstream in `conf/upstreams.yaml`, or rely on the live Codex/ChatGPT
usage reader for the `codex` subscription entry (reads `~/.codex/auth.json`,
percent-of-window only, read-only). `GET /compute/budget` reports local
hardware headroom plus every provider's `ok`/`warn`/`exhausted` status.
`POST /routing/request` and the MCP `route_task` tool drop exhausted
upstreams from candidate selection and flag near-limit ones — recommend-only,
they never forward a prompt themselves. See `docs/COMPUTE-BUDGET.md` for
config examples, the Codex live-usage path, and the dashboard's Compute
Providers tab.

## Security

- Binds to `127.0.0.1` by default.
- Set `A0_LMM_ROUTER_API_KEY` to require `Authorization: Bearer <key>` on
  everything except `/health`.
- Public binds (anything not loopback) **refuse to start** without an API key
  unless you explicitly set `A0_LMM_ROUTER_ALLOW_PUBLIC_NO_AUTH=1`.
- Config previews redact secrets. Prompts are not logged.

See `.env.example` for the full environment surface.

## Connecting harnesses

Hermes and Pi each receive one host URL. Agent Zero is the only current
exception and receives separate Docker-reachable chat and utility URLs:

```text
Hermes:             http://127.0.0.1:9000/harnesses/hermes/v1
Pi:                 http://127.0.0.1:9000/harnesses/pi/v1
Agent Zero chat:    http://host.docker.internal:9000/harnesses/agent_zero/chat/v1
Agent Zero utility: http://host.docker.internal:9000/harnesses/agent_zero/utility/v1
Claude Code local:  http://127.0.0.1:9000/harnesses/claude_code_local/v1 (through LiteLLM)
```

Use model ID `local` in the consumers. The router ignores that compatibility
label and forwards the model pinned in `conf/harnesses.yaml`. See
`docs/HARNESSES.md` for the setup contract.

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
`model_card`, `providers_list`, `route_preview`, `compute_budget`,
`route_task` - plus admin tools
(`start_fleet`, `start_slot`, `stop_slot`) **only** when
`MCP_ALLOW_MUTATING_TOOLS=1`. Bearer auth is on by default; inspect with
`npx @modelcontextprotocol/inspector http://127.0.0.1:8095/mcp`.
MCP calls the router at `A0_LMM_ROUTER_BASE_URL` (default
`http://127.0.0.1:9000`) and reuses `A0_LMM_ROUTER_API_KEY`.

See `docs/INTEGRATIONS.md` for Claude Code MCP, Open WebUI, Dify, Aider, and
Vercel AI SDK style setup snippets.

## Development

```powershell
.venv\Scripts\python -m pytest tests/ -q     # full suite
```

Pull requests and pushes to `main` run the same hermetic suite in GitHub
Actions; no fleet, GPU, Docker daemon, or network is required.

See `CONTRIBUTING.md` and `docs/development/git-workflow.md` for branch
naming, merge, cleanup, and agent handoff rules.

This repo follows the [DOX](https://github.com/agent0ai/dox) living-docs
convention: every significant directory has an `AGENTS.md` contract. Read the
local contract before editing an area; update it when behavior changes.

## License

MIT

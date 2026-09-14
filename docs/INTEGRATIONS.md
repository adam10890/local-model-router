# Integration snippets

Use a dedicated harness endpoint when the client should always receive one
pinned model. Keep the generic endpoint for scripts that intentionally use
aliases or automatic routing:

```text
http://127.0.0.1:9000/v1
```

## Built-in agent library

Install the optional runner with `pip install -e ".[agents]"` and set the
router's self-call URL before starting the service:

```text
A0_LMM_ROUTER_AGENT_BASE_URL=http://127.0.0.1:9000/v1
```

Discover the built-in agents through `GET /agents`, then send supplied task
context to `POST /agents/{id}/runs`:

```powershell
curl http://127.0.0.1:9000/agents
curl -X POST http://127.0.0.1:9000/agents/implementation-plan/runs `
  -H "Content-Type: application/json" `
  -d '{"input":"Plan this change: ..."}'
```

The runner sends `X-App-Id: agent_library` and the configured routing intent
to `/v1/chat/completions`. `local_only: true` in `conf/agents.yaml` prevents
auto-upstream fallback; otherwise it remains subject to the router's
local-first `A0_LMM_ROUTER_AUTO_UPSTREAMS=1` policy.

## Hermes

Checked against Hermes Agent **v0.21.0**
([`v2026.8.31`](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.8.31),
current latest as of 2026-09-01) and the official
[AI Providers](https://hermes-agent.nousresearch.com/docs/integrations/providers)
guide. Hermes is a **client** of Imperium (same class as Agent Zero / Pi): it
owns tools, memory, the agent loop, and the user's `/model` selection; Imperium
loads and serves the corresponding local llama.cpp profile over
OpenAI-compatible `/v1`.

Official Hermes rule: any server with `/v1/chat/completions` works via
`provider: custom` (or a named entry under `providers:`). `config.yaml` owns
the provider default; legacy `LLM_MODEL` in `.env` is removed upstream. For a
running Hermes session, Imperium reads the session selection from `state.db`
and uses the config default only when no model is loaded on a cold start.

On Windows the install lives under `%LOCALAPPDATA%\hermes` (config:
`%LOCALAPPDATA%\hermes\config.yaml`). Pin Imperium with a single harness
`default` connection so this URL works:

```text
http://127.0.0.1:9000/harnesses/hermes/v1
```

Recommended `config.yaml` shape (matches upstream custom-endpoint docs):

```yaml
model:
  default: local
  provider: custom
  base_url: http://127.0.0.1:9000/harnesses/hermes/v1
  api_key: local   # or Imperium's A0_LMM_ROUTER_API_KEY when auth is on
  context_length: 65536  # replace with effective_n_ctx observed after load
  supports_vision: false
```

Notes:

- Imperium ignores the compatibility label in the request. The Hermes session
  selection (`desired`), the connection pin, and the GGUF proven healthy by
  the live process (`loaded`) are separate status fields. The connection pin
  changes in memory only after the selected profile has loaded successfully;
  normal swaps do not rewrite `conf/harnesses.yaml`.
- The newest recognized open, non-archived session active in the last 15
  minutes determines `desired`. With no recent session, Imperium keeps a
  healthy loaded model; on a cold start it uses the profile default.
- Set `context_length` to `effective_n_ctx` observed in `/ui/status` or the
  Hermes connection's `/v1/models` metadata. Imperium first attempts a
  `/props` probe and then positive live `/v1/models` metadata; `/props` is a
  probe used by Imperium, not a guaranteed llama.cpp schema. When no effective
  value is measured it is `Unknown`, and the configured 128K request must not
  be treated as measured. Hermes 0.21.0 refuses configurations below its 64K
  minimum, so a stale 32768 default is not a valid substitute.
- Prefer `discover_models: false` on a dedicated pin; discovery noise has been
  observed against paths like `/harnesses/hermes/api/v1` (404) while
  `/harnesses/hermes/v1` succeeds.
- During drain, stop, load, restore, or retry backoff, new Hermes requests
  receive HTTP 503 with OpenAI error code `gpu_swap_in_progress` and a
  `Retry-After` header. Imperium refreshes session choice when a Hermes request
  arrives and again after local queue admission, but adds no webhook or
  Hermes-specific API. In the 2026-09-13 controlled run, Hermes 0.21.1 showed
  the retry delay and retried three times, then stopped before the cold load
  completed; a manual resend after readiness passed. The integration is
  therefore `Partial` for unattended `/model` waiting.
- An explicit `max_tokens` or `max_completion_tokens` is preserved. If neither
  is sent, the Ornith profile defaults to 16,384 output tokens. A
  `finish_reason=length` remains visible and the dashboard warns that the
  answer may be incomplete. The OBLITERATED profile keeps reasoning disabled.
- The current profiles are text/tools only: `supports_vision: false` because no
  mmproj is loaded. Vision requires a separate VRAM test and profile change.
- WhatsApp model changes go through Hermes `/model`; there is no separate
  WhatsApp-to-Imperium API. The `pnp` workflow remains deferred until swapping
  is stable.
- Record your installed Hermes version in
  [`1.0-beta-evidence.md`](1.0-beta-evidence.md) when you smoke.

## Pi

Verified setup shape against Pi **v0.81.1**
([release](https://github.com/earendil-works/pi/releases/tag/v0.81.1)).
Record your installed Pi version in
[`1.0-beta-evidence.md`](1.0-beta-evidence.md) when you smoke.

In `~/.pi/agent/models.json`, set the `lmm-router` provider's `baseUrl` to:

```text
http://127.0.0.1:9000/harnesses/pi/v1
```

Keep `defaultProvider` as `lmm-router` in `~/.pi/agent/settings.json` and set
`defaultModel` to the provider model ID `local`. The old direct ports
`8080`/`8088` bypass the router and should be removed from active providers.

```json
{
  "providers": {
    "lmm-router": {
      "baseUrl": "http://127.0.0.1:9000/harnesses/pi/v1",
      "api": "openai-completions",
      "models": [{"id": "local", "name": "LMM Router"}]
    }
  }
}
```

## Agent Zero

Use two OpenAI-compatible provider entries:

```text
Chat:    http://host.docker.internal:9000/harnesses/agent_zero/chat/v1
Utility: http://host.docker.internal:9000/harnesses/agent_zero/utility/v1
Model:   local
```

Agent Zero may use the generic router embedding endpoint at
`http://host.docker.internal:9000/v1/embeddings` with model `embedding`.

## Hermes planner + Pi workers

The optional Work Pages pilot lets Hermes create a dependency plan and Pi
workers atomically claim, log, complete, or block individual steps. It does not
launch workers. See [WORK_PAGES.md](WORK_PAGES.md) for the API contract and the
ready-to-copy integration packages.

## Claude Code local mode (optional)

Do not change the normal cloud Opus configuration. Install and run LiteLLM
Proxy as the Anthropic-Messages translator, mapping `openai/local` to:

```text
http://127.0.0.1:9000/harnesses/claude_code_local/v1
```

Then launch a separate shell with `ANTHROPIC_BASE_URL` pointing to LiteLLM.
The dashboard's Claude Code (local) card emits both configuration blocks.

Set `A0_LMM_ROUTER_API_KEY` in the router when exposing it beyond loopback,
then use the same value as the client API key. Prompt bodies are forwarded to
the selected model server but are not stored in router telemetry.

## Claude Code MCP

Run the MCP server:

```powershell
python -m local_model_router mcp
```

Set `A0_LMM_ROUTER_BASE_URL` when the router is not at
`http://127.0.0.1:9000`; MCP reuses `A0_LMM_ROUTER_API_KEY` for bearer auth.

Example MCP client entry:

```json
{
  "mcpServers": {
    "local-model-router": {
      "url": "http://127.0.0.1:8095/mcp"
    }
  }
}
```

Useful read-only tools: `list_models`, `model_card`, `providers_list`,
`route_preview`, `compute_budget`, and `route_task`. The last two are
budget-aware (see `docs/COMPUTE-BUDGET.md`) but still recommend-only — they
report which model would serve the task and why, the agent still makes the
call itself. Mutating fleet tools require `MCP_ALLOW_MUTATING_TOOLS=1`.

## Open WebUI

Add an OpenAI-compatible connection:

```text
Base URL: http://127.0.0.1:9000/v1
API Key:  local
Model:    auto
```

Use exact aliases such as `chat`, `fast`, `coder`, or `utility` when you want
predictable lanes. Use `auto` when you want capability-aware routing.

## Dify

Add a custom OpenAI-compatible provider:

```text
Endpoint URL: http://127.0.0.1:9000/v1
API Key:      local
Model name:   auto
```

For a configured upstream provider, use namespaced models such as
`ollama/llama3.3:70b`.

## Aider

```powershell
$env:OPENAI_API_BASE="http://127.0.0.1:9000/v1"
$env:OPENAI_API_KEY="local"
aider --model openai/coder
```

Aider does not need a custom header for basic use. If you need per-app policy,
configure the default app profile or run Aider through a wrapper that adds
`X-App-Id: aider`.

## Vercel AI SDK style usage

```ts
import { createOpenAI } from "@ai-sdk/openai";
import { generateText } from "ai";

const router = createOpenAI({
  baseURL: "http://127.0.0.1:9000/v1",
  apiKey: "local",
});

const result = await generateText({
  model: router("auto"),
  prompt: "Summarize this locally.",
});
```

## Route diagnostics

Use the read-only catalog before changing client settings:

```powershell
curl http://127.0.0.1:9000/routing/models
curl http://127.0.0.1:9000/routing/models/auto
curl http://127.0.0.1:9000/routing/analytics
curl http://127.0.0.1:9000/routing/evaluations
```

Refresh local model measurements manually before comparing ranked candidates:

```powershell
python -m local_model_router evaluate-models
```

Use dry-run routing for a specific requirement:

```powershell
curl -X POST http://127.0.0.1:9000/routing/request `
  -H "Content-Type: application/json" `
  -d "{\"task_type\":\"tool_calling\",\"requires_tools\":true,\"estimated_tokens\":12000,\"routing_strategy\":\"fastest\"}"
```

# Integration snippets

Use a dedicated harness endpoint when the client should always receive one
pinned model. Keep the generic endpoint for scripts that intentionally use
aliases or automatic routing:

```text
http://127.0.0.1:9000/v1
```

## Hermes

In `%LOCALAPPDATA%\hermes\config.yaml`, keep provider `lmm-router` and change
both `model.base_url` and `providers.lmm-router.base_url` to the dedicated URL.
Set `model.default`, `providers.lmm-router.default_model`, and its `models`
entry to `local`:

```yaml
model:
  default: local
  provider: lmm-router
  base_url: http://127.0.0.1:9000/harnesses/hermes/v1
providers:
  lmm-router:
    base_url: http://127.0.0.1:9000/harnesses/hermes/v1
    default_model: local
    models: [local]
```

Keep the existing API-key value; use `local` only when router auth is off.

## Pi

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

Keep Agent Zero's embedding provider unchanged until the router exposes an
embeddings endpoint.

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

Useful read-only tools: `list_models`, `model_card`, `providers_list`, and
`route_preview`. Mutating fleet tools require `MCP_ALLOW_MUTATING_TOOLS=1`.

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
```

Use dry-run routing for a specific requirement:

```powershell
curl -X POST http://127.0.0.1:9000/routing/request `
  -H "Content-Type: application/json" `
  -d "{\"task_type\":\"tool_calling\",\"requires_tools\":true,\"estimated_tokens\":12000,\"routing_strategy\":\"fastest\"}"
```

# Harness Endpoints Design

## Goal

Give every AI harness a stable identity, one pinned compute model, and one
dedicated connection URL. The router owns the compute mapping and emits exact
consumer-side setup instructions. The harness owns its internal agents, roles,
and workflows.

Agent Zero is the only current exception: it has separate `chat` and `utility`
connections because Agent Zero configures those as distinct model providers.

## Terminology

- **Harness:** a client runtime such as Agent Zero, Hermes, Pi, or Claude Code.
- **Agent:** an execution/session/persona created inside a harness.
- **Connection:** a router URL pinned to one compute model.
- **Model:** the concrete upstream or local model serving a connection.

The router does not infer or model a harness's internal roles. Generic harness
profiles have one `default` connection. Agent Zero has `chat` and `utility`.

## Canonical Configuration

`conf/harnesses.yaml` becomes the source of truth. During one compatibility
release, the loader falls back to `conf/apps.yaml` and `GET /apps` aliases
`GET /harnesses`. New writes and documentation use harness terminology only.

```yaml
harnesses:
  hermes:
    display_name: Hermes
    kind: hermes
    protocol: openai
    location: host
    connections:
      default:
        model: dmr/huggingface.co/deepreinforce-ai/ornith-1.0-9b-gguf:Q8_0

  agent_zero:
    display_name: Agent Zero
    kind: agent_zero
    protocol: openai
    location: docker
    connections:
      chat:
        model: dmr/huggingface.co/deepreinforce-ai/ornith-1.0-9b-gguf:Q8_0
      utility:
        model: utility_cpu
```

Secrets and API keys never enter this file. Harness IDs and connection names
use lowercase ASCII letters, digits, hyphens, and underscores.

## Dedicated URLs

Host harnesses receive loopback URLs:

```text
http://127.0.0.1:9000/harnesses/hermes/v1
http://127.0.0.1:9000/harnesses/pi/v1
```

Docker harnesses receive the same routes through Docker Desktop's host alias:

```text
http://host.docker.internal:9000/harnesses/agent_zero/chat/v1
http://host.docker.internal:9000/harnesses/agent_zero/utility/v1
```

For a single-connection harness, `/harnesses/{id}/v1` resolves `default`.
For a multi-connection harness, the connection name is explicit:
`/harnesses/{id}/{connection}/v1`.

The path is authoritative. A request header or body field cannot change the
harness, connection, or pinned model. The request's required OpenAI `model`
field is accepted for client compatibility but replaced before forwarding.
The generic `/v1` endpoints retain their current alias and auto-routing
behavior.

OpenAI-format connections expose:

- `GET .../v1/models`
- `POST .../v1/chat/completions`

`GET /harnesses/{id}` returns a secret-free setup manifest. Unknown harnesses
or connections return 404 with an explainable error code.

## Claude Code Local Mode

Claude Code remains cloud Opus by default. A separate optional local launcher
sets `ANTHROPIC_BASE_URL` and a local model identifier without modifying the
normal global Claude Code settings.

Claude Code speaks Anthropic Messages (`/v1/messages`), not OpenAI Chat
Completions. The first implementation uses LiteLLM Proxy's Anthropic endpoint
rather than implementing Claude's evolving beta/tool protocol inside the
router. The dashboard labels this dependency and emits the LiteLLM plus Claude
Code configuration together.

## Runtime Placement

Harness location and model-server location are independent:

- Agent Zero runs in Docker and uses `host.docker.internal`.
- Hermes, Pi, and Claude Code run on Windows and use `127.0.0.1`.
- Docker Model Runner continues serving NVIDIA/DMR models.
- Native llama.cpp continues serving the AMD Vulkan utility worker.
- The router remains a single host-native gateway.

This hybrid layout avoids moving models solely to match harness placement.
Model performance is governed primarily by the inference engine, GPU backend,
offload, quantization, context, batching, and model-file location. Networking
through the router is negligible relative to token generation.

## Dashboard

The existing visual language remains unchanged. Connect and Harnesses merge
into one fully interactive **Harnesses** screen.

Each harness card shows:

- display name, kind, location, and protocol
- one row per connection with pinned model and copyable URL
- router endpoint health and last-seen request time
- exact consumer configuration generated for that harness kind
- `Copy setup` and `Verify` actions

`Add harness` is a guided form. It defaults to one connection and only exposes
additional connections as an advanced action. The Agent Zero template creates
`chat` and `utility` automatically. Saving requires authenticated, explicitly
enabled config writes; otherwise the UI produces a YAML preview and CLI
command without mutating disk.

## Setup Contract

Creating a harness produces one manifest containing:

1. harness ID, kind, location, and protocol
2. connection name, pinned model, and dedicated base URL
3. whether an API key is required, without returning its value
4. exact consumer file/setting names and a copyable configuration block
5. a safe smoke command
6. verification state: `not_seen`, `seen`, or `verified`

The router records `last_seen` from real requests. It never claims that a
consumer is configured merely because instructions were generated.

## Existing Harness Migration

- **Agent Zero:** back up its model config; replace cloud chat/utility entries
  with the two Docker-reachable router connections. Keep its Hugging Face
  embedding model unchanged until the router supports embeddings.
- **Hermes:** replace the generic router URL and exact model with its dedicated
  URL and stable client model label.
- **Pi:** replace stale direct ports 8080/8088 with its dedicated router URL and
  one pinned model.
- **Claude Code:** preserve cloud Opus; add a separate `claude-local` launcher
  backed by the translation adapter.

Every external config edit gets a timestamped backup and a post-change smoke.

## Security And Failure Behavior

- Config-writing endpoints require bearer authentication and
  `A0_LMM_ROUTER_ENABLE_CONFIG_WRITES=1`.
- Writes are atomic and retain a timestamped backup.
- Generated output never contains an API key or prompt body.
- A configured but unavailable model returns an explainable 503; it does not
  silently route to another harness model.
- The dashboard distinguishes router down, adapter down, model unavailable,
  and consumer not yet seen.

## Verification

Tests cover schema loading and legacy migration, path validation, pinned-model
enforcement, unknown harnesses/connections, auth and write gating, generated
setup manifests, UI rendering, Agent Zero's two connections, and hermetic
smokes for migrated consumer configurations. The full suite remains runnable
without a live fleet, GPU, Docker, or network.

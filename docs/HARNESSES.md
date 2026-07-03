# Harness connections

A **harness** is a client runtime such as Hermes, Pi, Agent Zero, or Claude
Code. An **agent** is a session or persona created inside that runtime. The
router assigns compute to harness connections; it does not model the
harness's internal roles.

## Contract

`conf/harnesses.yaml` is the source of truth. A normal harness has one
`default` connection pinned to one concrete model:

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
```

Agent Zero is the current exception because its consumer settings expose
separate chat and utility providers:

```yaml
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

IDs use lowercase ASCII letters, digits, `_`, or `-`. API keys never belong
in this file.

## URLs

Single-connection host harnesses use:

```text
http://127.0.0.1:9000/harnesses/hermes/v1
http://127.0.0.1:9000/harnesses/pi/v1
```

Agent Zero runs in Docker, so its named connections use:

```text
http://host.docker.internal:9000/harnesses/agent_zero/chat/v1
http://host.docker.internal:9000/harnesses/agent_zero/utility/v1
```

Each base URL provides `GET /models` and `POST /chat/completions`. Clients may
send any compatibility model name; the path's pinned model is authoritative.
An unavailable pinned target returns `503 harness_model_unavailable` and does
not fail over to a different harness model. Generic `/v1` routing is unchanged.

## Add a harness

1. Choose a stable lowercase harness ID.
2. Choose `host` or `docker` based on where the harness process runs.
3. Pin one upstream model name or local fleet `model_id`.
4. Add it in the dashboard's **Harnesses → Add harness** form.
5. Copy the generated consumer setup and verify its `/models` endpoint.
6. Send a real completion and confirm `last_seen` changes.

Dashboard saving is intentionally off by default. To enable it, configure a
router API key and restart with:

```text
A0_LMM_ROUTER_API_KEY=<your local secret>
A0_LMM_ROUTER_ENABLE_CONFIG_WRITES=1
```

Writes are atomic and retain a timestamped `harnesses.yaml.*.bak` copy. With
writes disabled the form only generates a YAML preview.

## Compatibility

If `conf/harnesses.yaml` does not exist, one compatibility release derives
harness connections from the old `roles` entries in `conf/apps.yaml`.
`conf/apps.yaml` still controls generic `/v1` app policy; new dedicated
connections must be declared in `conf/harnesses.yaml`.

## Claude Code

Claude Code remains on cloud Opus by default. Its native gateway contract is
Anthropic Messages (`/v1/messages`), while harness endpoints in this release
are OpenAI Chat Completions. A separate optional local launcher should put
LiteLLM Proxy in front of the Claude-local harness; do not replace the normal
global Claude Code configuration. The router-side connection is:

```text
http://127.0.0.1:9000/harnesses/claude_code_local/v1
```

The dashboard emits the LiteLLM model mapping and isolated PowerShell launcher.

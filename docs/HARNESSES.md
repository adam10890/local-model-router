# Harness connections

A **harness** is a client runtime such as Hermes, Pi, or Claude Code. An
**agent** is a session or persona created inside that runtime. The router
assigns compute to harness connections; it does not model the harness's
internal roles.

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
        # Prefer a local llama.cpp slot id when that slot is healthy with
        # mmproj. Until then, DMR Ornith is the working text pin.
        model: dmr/huggingface.co/deepreinforce-ai/ornith-1.0-9b-gguf:Q8_0
```

Prefer a **local** llama.cpp slot (`model: ornith` with `mmproj_path`) for
Vision once that slot is healthy. DMR may serve text/tools but Imperium does
not load mmproj into DMR. LiteLLM is only for the optional Claude Code
Anthropic bridge, not a substitute for the local fleet.

IDs use lowercase ASCII letters, digits, `_`, or `-`. API keys never belong
in this file.

## URLs

Single-connection host harnesses use:

```text
http://127.0.0.1:9000/harnesses/hermes/v1
http://127.0.0.1:9000/harnesses/pi/v1
```

Each base URL provides `GET /models` and `POST /chat/completions`. Clients may
send any compatibility model name; the path's pinned model is authoritative.
An unavailable pinned target (no connection, timeout, or unloaded slot)
returns `503 harness_model_unavailable` and does not fail over. Capability
mismatches such as missing mmproj / unsupported image input return
`upstream_capability_missing` instead of a generic unavailable error.
Generic `/v1` routing is unchanged.
Pinned requests use the target's admission lane: `local` for a local model or
`upstream:<name>` for a capacity-managed upstream. The response includes
`X-A0-Request-ID` and `X-A0-Admission-Lane`; a full lane returns HTTP 429
without changing the pinned model.

Verify every configured connection from the router host with:

```powershell
python .\scripts\smoke_harnesses.py
```

Pass `--api-key <key>` when router authentication is enabled. The command
checks `/models` and sends one short completion per connection.

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

If `conf/harnesses.yaml` does not exist, one compatibility release can derive
harness connections from legacy `roles` entries in `conf/apps.yaml`.
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

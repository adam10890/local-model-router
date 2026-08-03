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

## Runtime compatibility policy

Before adding or updating any harness, verify its integration against its
official repository and documentation. Check both the latest stable release
and the version actually installed; the installed version is authoritative
for configuration, while the latest release identifies upgrade gaps. Record
the verified release or commit in the matrix below, validate the provider
names, settings schema, endpoints, authentication, model IDs, and
stream/tool behavior, then rerun the harness tests. Repeat the check whenever
the runtime or its setup manifest changes. A generic "OpenAI-compatible"
claim alone is not sufficient evidence of compatibility.

### Version matrix (2026-08-03)

| Harness | Official repo | Current stable checked | Installed (operator) | Router path | Setup doc |
| --- | --- | --- | --- | --- | --- |
| Agent Zero | [agent0ai/agent-zero](https://github.com/agent0ai/agent-zero) | **v2.7** (`87e1e591…`) verified | fill when validating | `/harnesses/agent_zero/{chat,utility}/v1` | section below |
| Hermes | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) | **v0.20.0** (`v2026.8.3`) checked against docs | fill when validating | `/harnesses/hermes/v1` | [INTEGRATIONS.md](INTEGRATIONS.md#hermes) |
| Pi | [earendil-works/pi](https://github.com/earendil-works/pi) (`badlogic/pi-mono`) | **v0.81.1** checked against docs | fill when validating | `/harnesses/pi/v1` | [INTEGRATIONS.md](INTEGRATIONS.md#pi) |
| Claude Code (local) | Anthropic Claude Code + LiteLLM bridge | cloud Opus default; local via LiteLLM | fill when validating | `/harnesses/claude_code_local/v1` | section below |

"Checked against docs" means the Imperium setup snippet was reviewed against
that release's OpenAI-compatible / provider settings. It is not a substitute
for recording the operator's installed version and a live smoke Pass.

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

Pass `--api-key <key>` when router authentication is enabled. By default the
command checks `/models`, one short completion, and one streaming completion
per connection. Add `--tools` to include a no-op tools array; add `--no-stream`
to skip streaming.

## Agent Zero 2.7

This setup was verified against the official
[`agent0ai/agent-zero` v2.7 release](https://github.com/agent0ai/agent-zero/releases/tag/v2.7)
at commit `87e1e591e1ba2e8b1a19d34e134fcae490c8dded`.

In Agent Zero, open **Settings → Agent → Models → Edit presets** and create or
edit a preset with these values:

| Slot | Provider | Model | API base |
| --- | --- | --- | --- |
| Main model | `other` (`Other OpenAI compatible`) | `local` | `http://host.docker.internal:9000/harnesses/agent_zero/chat/v1` |
| Utility model | `other` (`Other OpenAI compatible`) | `local` | `http://host.docker.internal:9000/harnesses/agent_zero/utility/v1` |

Under **External Services → Other OpenAI-compatible API keys**, use the value
of `A0_LMM_ROUTER_API_KEY`; use `local` only when router authentication is
disabled. Provider `other` sets Agent Zero's `a0_api_mode` to `chat`, matching
Imperium's Chat Completions contract without probing `/v1/responses`. Set each
Agent Zero context window no higher than the corresponding pinned model.

Agent Zero runs inside Docker, so `127.0.0.1` would address the container
itself. Keep the `host.docker.internal` URLs above and ensure port 9000 is
reachable from the container before running `scripts/smoke_harnesses.py`.

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

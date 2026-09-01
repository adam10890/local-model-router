# Standalone Provider Runbook

The standalone router is a local OpenAI-compatible provider and Fleet Manager
control plane: agent-aware state, bounded queueing, hardware telemetry, and
fleet status endpoints. It does not start or stop llama.cpp containers unless
fleet control is explicitly enabled.

## Endpoints

- `GET /health` remains open for probes.
- `POST /routing/request` returns the routing decision for an intent payload.
- `POST /v1/chat/completions` forwards non-streaming and streaming requests to
  the selected llama.cpp slot.
- `GET /agents` lists built-in prompt-backed agents without their prompts;
  `POST /agents/{id}/runs` executes one through the router's own Chat
  Completions endpoint.
- `GET /harnesses` and `GET /harnesses/{id}` emit secret-free setup manifests.
  Dedicated `.../v1/models` and `.../v1/chat/completions` paths pin one model
  per connection; see `HARNESSES.md`. Hermes Vision pins a local llama.cpp
  slot (for example `ornith` with `mmproj_path`); DMR is optional text/tools
  only and is not managed for multimodal projectors.
- `GET /fleet/status` returns the legacy local `queue`, per-lane `queues`,
  agent, request, slot, state, and local hardware summaries.
- `GET /fleet/agents` lists registered/observed agents.
- `POST /fleet/agents/register` registers an agent identity manually.
- `GET /compute/budget` returns local hardware headroom plus per-provider
  usage vs declared/live limits; see `COMPUTE-BUDGET.md`.
- `GET /routing/evaluations` returns the latest safe model-evaluation snapshot.
- `GET /backends` reports each upstream's capacity mode, limits, and any
  configuration error.
- `GET /diagnostics/report` returns an authenticated, read-only, versioned
  support report. It shares doctor checks and stable check codes with
  `imperium doctor --json`, and adds readiness, safe slot/runtime failure
  codes, CPU/RAM/GPU telemetry, backend, active-slot count, fleet-control
  state, and auth-enabled state.

When `A0_LMM_ROUTER_API_KEY` is set, every endpoint except `/health` requires:

```text
Authorization: Bearer <key>
```

## Sanitized Diagnostics

Use Advanced → Diagnostics to run current checks and download
`imperium-diagnostics-<UTC>.json`, or request the report directly:

```powershell
Invoke-RestMethod http://127.0.0.1:9000/diagnostics/report `
  -Headers @{ Authorization = "Bearer $env:A0_LMM_ROUTER_API_KEY" }
```

The endpoint always returns HTTP 200 for complete and partial diagnostic
collection. Inspect `ok`, `doctor.checks`, and `collection_errors`; each
partial failure has a stable sanitized code. Missing or invalid Bearer auth is
the only diagnostic condition that returns HTTP 401.

The report is built from an allowlist. It excludes configuration and model
paths, API keys, request headers, environment values, URL credentials,
prompts, responses, logs, and raw exception text. Export is evidence only: it
does not enable fleet control or start, stop, repair, or reconfigure a server.

## Hardware Telemetry

`GET /fleet/status` preserves the existing `vram` object:

```json
{
  "total_gb": 24.0,
  "used_gb": 6.0,
  "available_gb": 18.0,
  "source": "nvidia-smi"
}
```

It also returns an additive `compute` object with a Unix timestamp, per-GPU
MiB/utilization/temperature fields, CPU utilization, and RAM totals in MiB.
NVIDIA data comes from `nvidia-smi`; CPU and RAM data come from `psutil`.
Snapshots are cached for five seconds and collected outside the ASGI event
loop. If a probe fails, fleet status remains HTTP 200 and reports explicit
unavailable values. Hardware telemetry is local to the router host; remote
fleet hosts are not probed.

When opt-in fleet control has already initialized a managed slot,
`GET /fleet/status` adds a safe `runtime` object to that slot. It contains
only `running`, `healthy`, `failure_code`, `exit_code`, `restart_count`, and
`uptime_s`; commands, environment variables, model paths, logs, and prompts
are never included.

## Windows

From the repository root:

```powershell
.\scripts\run_provider.ps1 -InstallDeps
```

With a local API key:

```powershell
.\scripts\run_provider.ps1 `
  -HostName 127.0.0.1 `
  -Port 9000 `
  -ApiKey "change-me" `
  -InstallDeps
```

Release smoke (sanitized JSON; live model required):

```powershell
.\scripts\smoke_provider.ps1 -ApiKey "change-me" -RequireLive -JsonOutput output\evidence\provider.json
python .\scripts\smoke_harnesses.py --api-key "change-me" --rc --harness hermes --json-output output\evidence\hermes.json
```

`SETUP.bat` installs the agent runner extra. `START.bat` derives
`A0_LMM_ROUTER_AGENT_BASE_URL` from the configured bind URL unless `.env`
overrides it; `STOP.bat` reads the same `OBSERVER_PORT` before stopping the
router. On Windows Terminal it validates and stops the router process that
owns that port; it never terminates an unrelated listener.

## WSL Or Linux Server

From the repository root:

```bash
./scripts/run_provider.sh --install-deps
```

With a local API key:

```bash
./scripts/run_provider.sh \
  --host 127.0.0.1 \
  --port 8096 \
  --api-key "change-me" \
  --install-deps
```

Release smoke:

```bash
./scripts/smoke_provider.sh --api-key "change-me" --require-live --json-output output/evidence/provider.json
python scripts/smoke_harnesses.py --api-key "change-me" --rc --harness hermes --json-output output/evidence/hermes.json
```

The provider result contains statuses and HTTP codes, never response or prompt
bodies. Harness schema v2 distinguishes endpoint evidence from installed and
stable versions and a real-client canary. RC mode checks models, chat, stream,
an actual tool call, the installed version, and the client canary for every
explicitly required harness. The official stable lookup is compared and
reported when available but never makes an otherwise valid RC network-bound.
Missing harnesses and missing client evidence are failures; only Hermes has a
client canary in this release.

## Public Bind Safety

Public binds such as `0.0.0.0` require `A0_LMM_ROUTER_API_KEY`. The service
refuses to start otherwise. For a temporary isolated lab you may pass
`-AllowPublicNoAuth` or `--public-no-auth`, which sets
`A0_LMM_ROUTER_ALLOW_PUBLIC_NO_AUTH=1`.

Do not use public no-auth mode on a shared network.

## Environment Example

See `conf/standalone_provider.env.example`.

Minimum useful values:

```text
OBSERVER_HOST=127.0.0.1
OBSERVER_PORT=9000
A0_LMM_ROUTER_CONFIG=conf/llama_cpp_servers.yaml
A0_LMM_ROUTER_API_KEY=change-me
A0_FLEET_MAX_ACTIVE=1
A0_FLEET_MAX_QUEUE=32
# Router address used by the MCP bridge:
A0_LMM_ROUTER_BASE_URL=http://127.0.0.1:9000
# Full OpenAI-compatible URL used by built-in agent runs:
A0_LMM_ROUTER_AGENT_BASE_URL=http://127.0.0.1:9000/v1
```

## Dependency Map

Runtime dependencies are declared in `pyproject.toml` and installed by
`pip install -e .`: `aiohttp` for upstream forwarding, Starlette/Uvicorn for
HTTP serving, Pydantic for schemas, PyYAML for fleet configuration, and
`psutil` for cross-platform CPU/RAM telemetry. Optional Docker and MCP support
remain in the `[docker]` and `[mcp]` extras. Install `.[agents]` to enable the
Pydantic AI runner used by `POST /agents/{id}/runs`.

## Agent library

Agent runs call the router asynchronously through
`A0_LMM_ROUTER_AGENT_BASE_URL`, carry the configured role/task intent, and
have a 64 KiB input limit plus a 120-second timeout. Requests finish as
`completed`, `failed`, or `rejected`; `GET /routing/analytics` retains the
selected upstream and admission lane together with `app_id=agent_library`
and the agent id. Agent prompts are never returned by `/agents` or saved in
telemetry.

## Admission lanes

Local requests use the existing bounded `local` lane. An upstream with
`max_active` gets its own in-memory `upstream:<name>` lane; `max_queue`
defaults to 32. An upstream without `max_active` delegates capacity to the
provider and must omit `max_queue`. Invalid upstream entries remain visible
through `GET /backends` with `config_error` but never serve inference.

Admission is per process. Run one router worker when the configured limits
must be hard global limits. The router never starts or stops an upstream.
Every allocated chat response, including errors, carries
`X-A0-Request-ID` and `X-A0-Admission-Lane`.

## Model evaluation

Run the deterministic evaluator manually; it never executes model-generated
code and does not use an LLM judge:

```powershell
python -m local_model_router evaluate-models
```

Only reachable local models are evaluated. Results are stored in the existing
snapshot store, reused while the model/runtime/hardware fingerprint is
unchanged, and exposed safely through `GET /routing/evaluations` and
`GET /routing/models`. Prompts, responses, and sensitive content are not
stored. Use `--force` to refresh an unchanged model.

## Update Guide

1. Pull the updated standalone router files.
2. Re-run `scripts/run_provider.ps1 -InstallDeps` or
   `scripts/run_provider.sh --install-deps` to refresh Python dependencies.
3. Keep `conf/llama_cpp_servers.yaml` outside automated overwrites if it
   contains machine-specific host or port settings.
4. Restart the provider process.
5. Run the smoke script before pointing clients at the provider.

Managed Windows runtime updates query recent llama.cpp rolling releases and
accept only the newest build that supplies every expected backend asset with a
GitHub SHA-256 digest. `imperium rollback` restores the previous pinned runtime
and updates the managed configuration path with it.

## Local llama.cpp vs upstreams

Local GGUF inference is owned by the llama.cpp fleet
(`conf/llama_cpp_servers.yaml`), not by a local Ollama process.

| Path | Use for |
| --- | --- |
| Managed / subprocess `llama-server` | Local GGUF models (Hermes, Chat, `auto`) |
| `ollama_cloud` in `conf/upstreams.yaml` | Hosted / free Ollama Cloud models (`OLLAMA_API_KEY`) |
| Local `ollama` HTTP upstream | Optional only if you deliberately want that server; do not use it as the primary local GGUF path |

Recommended operator shape on Windows + NVIDIA:

1. Install a managed CUDA runtime (`imperium setup` / `SetupEngine.install_runtime("cuda12")`).
   Binary lands under `%LOCALAPPDATA%\Imperium\runtime\llama.cpp\versions\<tag>-cuda12`.
2. Set `global.backend: subprocess` and `global.llama_cpp_path` to that version directory.
3. Enable `A0_LMM_ROUTER_ENABLE_FLEET_CONTROL=1`, then
   `POST /fleet/slots/{id}/start` (or the dashboard Start control).
   With `global.auto_restart: true`, Imperium live-probes subprocesses it
   started and retries crashes up to `global.max_restart_attempts`; an explicit
   Stop remains stopped. `global.auto_start: false` still requires the first
   Start action after Imperium launches.
4. Keep `A0_LMM_ROUTER_ENABLED_UPSTREAMS` empty for local-only, or set
   `ollama_cloud` when cloud models are needed. Do not enable local `ollama`
   just to serve a GGUF that llama.cpp can load.

### High-VRAM single-model slot (pattern)

Pin one strong chat model with an absolute or models-dir-relative `model_path`,
`model_id` matching `conf/harnesses.yaml` / `conf/apps.yaml`, and flags that
favor GPU fill without starving KV cache:

```yaml
- id: slot_chat
  port: 8080
  role: chat
  model_id: chat_primary   # harness pin must match this id or the slot id
  model_path: your-model.gguf
  context_size: 32768
  gpu_layers: -1
  batch_size: 2048
  threads: 16
  parallel_slots: 1
  flash_attention: true
  fit: true
  fit_target_mib: 1024
  jinja: true
  reasoning_format: deepseek   # thinking models: final answer in content
  router_mode: false
  extra_args: [--cache-type-k, q8_0, --cache-type-v, q8_0, -ub, "512", --cont-batching]
```

Thinking / MoE models often need a large `max_tokens` budget so reasoning does
not consume the entire completion window (`finish_reason=length` with empty
`content`). The dashboard Chat path sends `max_tokens: 1024`; harness clients
should send enough tokens (commonly ≥2048) or rely on server-side floors when
present.

Unload any other GPU-resident copy of the same weights (for example a local
Ollama load) before starting the subprocess slot, or VRAM fit will fail.

## Client Base URL

Point OpenAI-compatible clients at:

```text
http://127.0.0.1:9000/v1
```

Use the configured API key as the client API key. For clients that only support
an API-key field, set it to the same value as `A0_LMM_ROUTER_API_KEY`.

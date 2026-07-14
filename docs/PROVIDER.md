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
  per connection; see `HARNESSES.md`.
- `GET /fleet/status` returns queue, agent, request, slot, state, and local
  hardware summaries.
- `GET /fleet/agents` lists registered/observed agents.
- `POST /fleet/agents/register` registers an agent identity manually.

When `A0_LMM_ROUTER_API_KEY` is set, every endpoint except `/health` requires:

```text
Authorization: Bearer <key>
```

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

Smoke test:

```powershell
.\scripts\smoke_provider.ps1 -ApiKey "change-me"
python .\scripts\smoke_harnesses.py --api-key "change-me"
```

`SETUP.bat` installs the agent runner extra. `START.bat` derives
`A0_LMM_ROUTER_AGENT_BASE_URL` from the configured bind URL unless `.env`
overrides it; `STOP.bat` reads the same `OBSERVER_PORT` before stopping the
router.

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

Smoke test:

```bash
./scripts/smoke_provider.sh --api-key "change-me"
python scripts/smoke_harnesses.py --api-key "change-me"
```

The harness smoke checks `/models` and one short completion for every
configured dedicated connection. It fails when a pinned model is unavailable,
so run it only when the configured harness fleet is expected to be online.

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
have a 64 KiB input limit plus a 120-second timeout. When auto-upstreams is
enabled, non-local-only agents may be recorded as `forwarded_upstream` in
`GET /routing/analytics`; those records retain `app_id=agent_library` and the
agent id. Agent prompts are never returned by `/agents` or saved in telemetry.

## Update Guide

1. Pull the updated standalone router files.
2. Re-run `scripts/run_provider.ps1 -InstallDeps` or
   `scripts/run_provider.sh --install-deps` to refresh Python dependencies.
3. Keep `conf/llama_cpp_servers.yaml` outside automated overwrites if it
   contains machine-specific host or port settings.
4. Restart the provider process.
5. Run the smoke script before pointing clients at the provider.

## Client Base URL

Point OpenAI-compatible clients at:

```text
http://127.0.0.1:9000/v1
```

Use the configured API key as the client API key. For clients that only support
an API-key field, set it to the same value as `A0_LMM_ROUTER_API_KEY`.

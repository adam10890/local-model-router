# Standalone Provider Runbook

Phase 9 packages the router service as a small local OpenAI-compatible
provider. It now also acts as the V1 Fleet Manager control plane: agent-aware
state, bounded queueing, and fleet status endpoints. It does not change Agent
Zero plugin behavior and it does not start or stop llama.cpp containers.

## Endpoints

- `GET /health` remains open for probes.
- `POST /routing/request` returns the routing decision for an intent payload.
- `POST /v1/chat/completions` forwards non-streaming and streaming requests to
  the selected llama.cpp slot.
- `GET /fleet/status` returns queue, agent, request, slot, and state summary.
- `GET /fleet/agents` lists registered/observed agents.
- `POST /fleet/agents/register` registers an agent identity manually.

When `A0_LMM_ROUTER_API_KEY` is set, every endpoint except `/health` requires:

```text
Authorization: Bearer <key>
```

## Windows

From the plugin root:

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
```

## WSL Or Linux Server

From the plugin root:

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
```

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
# For MCP clients that should call this service instead of BackendManager:
# A0_FLEET_MANAGER_BASE_URL=http://127.0.0.1:9000
```

## Dependency Map

Runtime Python dependencies are listed in `requirements.txt`:

- `aiohttp` for upstream forwarding to llama.cpp slots
- `mcp` for the plugin's MCP surface
- `pyyaml` for fleet config parsing

The standalone service also uses dependencies already present in Agent Zero's
Python environment, including Starlette, Pydantic, and Uvicorn. If running
outside the Agent Zero environment, install the A0 application dependencies or
use the A0 virtual environment.

## Update Guide

1. Pull or copy the updated plugin files.
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

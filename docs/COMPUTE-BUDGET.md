# Compute Budget

One place tracks all available compute — the local llama.cpp fleet plus
configured subscription/upstream providers — and routing stays aware of it.
Local hardware headroom comes from the same probe `GET /fleet/status` uses
(`local_model_router/helpers/compute_monitor.py`); provider headroom comes
from either a live usage reader (Codex today) or a declared rolling-window
limit checked against a local usage ledger.

## Declaring limits on an upstream

`conf/upstreams.yaml` entries gained two things: a `limits` list and a
`subscription` provider `type` for CLI-driven providers with no HTTP surface
at all.

```yaml
- name: codex
  type: subscription
  invoke: codex_cli
  default_model: gpt-5-codex
  enabled: false
  limits:
    - window: "5h"
      max_tokens: 1000000
    - window: "7d"
      max_requests: 5000
  notes: "OpenAI Codex CLI subscription. No HTTP surface — invoked as a local process; limits are declared from the published plan, not measured live."

- name: ollama_cloud
  type: openai_compatible
  base_url: https://ollama.com/v1
  api_key_env: OLLAMA_API_KEY
  enabled: false
  limits:
    - window: "1h"
      max_requests: 200
    - window: "1d"
      max_tokens: 5000000
  notes: "Ollama Cloud's hosted OpenAI-compatible endpoint. Declared limits count against the subscription plan since there's no live quota API."
```

- `type: subscription` has no `base_url` — `UpstreamConfig.serves_inference` is
  always `False` for it. `invoke` names the local runner (currently only
  `codex_cli` is recognized by the budget engine) and `default_model` is the
  model id assumed when none is given.
- `type: openai_compatible` upstreams can declare `limits` too (see
  `ollama_cloud` above) — the adapter type and the limits are independent.
- Each `limits:` entry is one rolling window: `window` is `"<int>h"` or
  `"<int>d"` (parsed by `parse_window()` in
  `local_model_router/upstreams/registry.py`), plus `max_tokens` and/or
  `max_requests`. Leave an axis unset (`0`/`None`) to mean "no cap declared
  there," not "cap of zero."
- These numbers are **hand-declared from the provider's published plan**, not
  measured or fetched from the provider — nothing here calls out to Codex or
  Ollama to ask what your actual cap is.
- A malformed `limits:` entry degrades to being dropped, not to failing the
  whole upstream — one typo doesn't take out the rest of the config.

## Live Codex usage

For the `codex` entry specifically (`invoke: codex_cli`), the budget engine
prefers a **live** read over the declared `limits:` math:
`local_model_router/helpers/codex_usage.py` reads the Codex CLI's own OAuth
credentials and asks ChatGPT's backend for the same 5h/7d rate-limit windows
the `codex` CLI shows itself.

- **Reads** `~/.codex/auth.json` (override the directory with `CODEX_HOME`).
  This file is created by running `codex login` — Imperium never performs
  auth itself, it only reads the credential Codex already wrote.
- If the access token is close to expiry it is refreshed in memory via
  `https://auth.openai.com/oauth/token`; the refreshed token is never written
  back to disk by this reader.
- The usage call hits `https://chatgpt.com/backend-api/codex/usage` (with two
  fallback paths). This is an **undocumented ChatGPT backend endpoint**, not a
  published API — it can change or disappear without notice.
- It reports **percent-used and reset time per window**, never absolute token
  counts — Codex doesn't expose those, and the router only needs the
  percentage to throttle.
- **Security:** the auth file is opened read-only. The access/refresh/id
  tokens are never logged, never included in an exception message, and never
  returned to a caller. Every failure mode (file missing, bad JSON, expired
  token with no refresh, network error, non-200 response) degrades to
  `{"available": False, "reason": "<short non-sensitive string>"}` — it never
  raises.

When the live read is unavailable (no `auth.json`, Codex not logged in,
network failure, etc.), the budget engine falls back to the declared `limits:`
math described above; if `codex` has no declared limits either, its status is
`"unknown"` with that same safe reason string.

## `GET /compute/budget`

Returns local hardware headroom plus every enabled-or-limited provider's
budget:

```json
{
  "ts": 1732000000.0,
  "local": {
    "available": true,
    "timestamp": 1732000000.0,
    "gpus": [{"id": 0, "name": "RTX 4090", "total_vram_mb": 24564, "used_vram_mb": 6000, "free_vram_mb": 18564, "utilization_pct": 12.0, "temperature_c": 52.0}],
    "cpu": {"utilization_pct": 8.4},
    "ram": {"total_mb": 65536, "used_mb": 20480, "available_mb": 45056, "utilization_pct": 31.3}
  },
  "providers": [
    {
      "provider": "codex",
      "kind": "subscription",
      "invoke": "codex_cli",
      "enabled": false,
      "status": "ok",
      "source": "live",
      "plan_type": "plus",
      "windows": [
        {"label": "5h", "used_percent": 42.0, "remaining_percent": 58.0, "window_seconds": 18000, "resets_at": "...", "source": "live"}
      ]
    },
    {
      "provider": "ollama_cloud",
      "kind": "openai_compatible",
      "invoke": "",
      "enabled": false,
      "status": "warn",
      "source": "ledger",
      "windows": [
        {"window": "1d", "used_tokens": 4200000, "max_tokens": 5000000, "used_requests": 0, "max_requests": null, "remaining": 800000, "pct": 84.0, "burn_per_hour": 120000, "exhausts_in_hours": 6.7, "source": "ledger"}
      ]
    }
  ]
}
```

Only providers that are `enabled: true` **or** declare `limits:` show up in
`providers` — a disabled provider with no limits contributes nothing.

`status` is one of:

| Status | Meaning |
|---|---|
| `ok` | below 80% of every declared/live window |
| `warn` | at or above 80% (`WARN_PCT`) on some window |
| `exhausted` | at or above 100% (`EXHAUSTED_PCT`) on some window |
| `tracked` | provider is enabled but declares no limits and has no live reader — no cap to compare against |
| `unknown` | a live reader is expected (`codex_cli`) but is unavailable, and no declared `limits:` fallback exists; `reason` carries a short, non-sensitive explanation |

`source` is `live` (Codex), `ledger` (declared limits, checked against the
local usage ledger), or `none`. A broken hardware probe degrades `local` to
`{}`; a broken usage source degrades that one provider's entry to `status:
"unknown"` rather than failing the whole request — `/compute/budget` does not
error because one provider's accounting broke.

## MCP tools: `compute_budget()` and `route_task()`

Both are **recommend-only** — like every other routing tool in this MCP
server, they never forward a prompt. The calling agent reads the
recommendation and makes the actual inference call itself.

- **`compute_budget()`** — thin proxy for `GET /compute/budget` above.
- **`route_task(task="", role="chat", est_input_tokens=0, est_output_tokens=0, quality="best_available")`**
  — like `route_preview`, but budget-aware: a provider whose window is
  `exhausted` is dropped from candidate selection, and one that's `warn` is
  kept but flagged. `task` is a free-text description forwarded as
  `metadata.task` (not used for routing decisions itself); `est_input_tokens`
  / `est_output_tokens` feed the context-fit estimate. `quality` is accepted
  and forwarded on the request but is not yet consumed by candidate scoring —
  use `routing_strategy` (`balanced_local` | `fastest` | `quality` |
  `economy`) for the existing quality-weighted strategy today.

Both call the router over HTTP (`A0_LMM_ROUTER_BASE_URL`, default
`http://127.0.0.1:9000`), same as every other MCP tool in
`local_model_router/mcp/router_bridge.py`.

## Budget-aware routing

`POST /routing/request` (`RoutingIntentRequest` /
`local_model_router/service/routing_intent.py`) gained three fields:

- `est_input_tokens: int = 0`
- `est_output_tokens: int = 0`
- `quality: str = ""` (accepted, not yet wired into scoring — see above)

When `estimated_tokens` is left unset, `est_input_tokens + est_output_tokens`
is used for context-window sizing instead.

Budget awareness only applies to **upstream candidates**, and only when
upstream candidates are in play at all — i.e. `A0_LMM_ROUTER_AUTO_UPSTREAMS=1`
is set, the request isn't `local_only`, and the role isn't `embed`/`embedding`.
Local slots are never dropped or flagged for budget reasons. When upstream
candidates are being considered, each one is checked against
`GET /compute/budget`'s provider statuses:

- `exhausted` → the candidate is removed from the pool entirely, and
  `upstream_budget_exhausted:<name>` is added to both `reason_codes` and
  `warnings`.
- `warn` → the candidate is kept, with `upstream_budget_low:<name>` added to
  `warnings` only.

The response's `budget` field is a lightweight map, not the full
`/compute/budget` snapshot — `{"<provider>": "<status>"}` for every provider
that was checked, e.g. `{"codex": "ok", "ollama_cloud": "exhausted"}`. When no
upstream candidates were in play, `budget` is `{}`; this is entirely inert
unless auto-upstreams routing is active.

## Local usage ledger

`local_model_router/helpers/usage_ledger.py` is an append-only JSONL ledger:
one file, one process (the Starlette service), events pruned after 35 days.
It exists for provider usage that is **neither available from a live API nor
proxied through Imperium's own forwarding** — e.g. calling Ollama Cloud
directly outside the router, or any other declared-limit provider without a
live reader. Imperium-proxied traffic through the router's own
`/v1/chat/completions` and the live Codex reader don't need it — they're
either counted by the router directly or read live.

- Path: `A0_USAGE_LEDGER_PATH` env var, default
  `<tempdir>/a0_lmm_router/usage_ledger.jsonl`.
- Write API: `record_usage(provider_id, tokens_in=0, tokens_out=0, requests=1, model="", source="")`.
- Read API used by the budget engine: `window_totals(provider_id, window_seconds, now=None)` →
  `{"tokens": n, "requests": n}`, summed over the trailing window.
- As shipped in this phase, nothing in the request-forwarding path calls
  `record_usage()` automatically — it's the accounting primitive the budget
  engine reads (`_declared_windows()` in `budget_engine.py`), but populating it
  for a given provider today means calling it directly (or from a future
  integration). Until something writes to it, declared-limit math for that
  provider will read as zero usage.
- Pruning is opportunistic: at most once per process per day, on the next
  `record_usage()` call.

## Dashboard: Compute Providers tab

The Advanced dashboard (`GET /ui#/advanced/providers`) adds a "Compute
Providers" tab next to Fleet/Routing/Configuration/Diagnostics. It calls
`GET /compute/budget` on load/refresh and renders one card per provider
(status, source, windows) plus a "This computer" card for the local hardware
snapshot. If the call fails, the tab shows a status banner
("Budget data is unavailable right now") instead of failing to render.

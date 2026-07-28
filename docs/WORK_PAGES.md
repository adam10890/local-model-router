# Work Pages pilot

Imperium's authenticated, observe-first orchestration surface can coordinate a
Hermes planner and short-lived Pi workers without launching processes itself.
It reuses the existing SQLite database and per-ticket workspaces; no second
queue or state store is introduced.

The pilot remains under `/orchestrator/*`, is hidden from the dashboard, and
returns `Deprecation: true`. It is a compatibility boundary, not the future
terminal/process execution surface.

## Contract

All routes except `/health` use the router bearer key when configured.

| Route | Purpose |
| --- | --- |
| `POST /orchestrator/plans` | Create one work page and its dependency graph |
| `GET /orchestrator/plans/{plan_id}` | Read page, steps, wake marker, and work document |
| `GET /orchestrator/tickets/{ticket_id}` | Read one step, DOX chain, and latest 200 events |
| `POST /orchestrator/tickets/{ticket_id}/claim` | Atomically claim or renew a lease |
| `POST /orchestrator/tickets/{ticket_id}/log` | Append an owned progress event |
| `POST /orchestrator/tickets/{ticket_id}/complete` | Complete an owned step |
| `POST /orchestrator/tickets/{ticket_id}/block` | Block an owned step and wake the planner |

Claim body:

```json
{
  "worker_id": "pi-code-1",
  "lease_seconds": 900
}
```

Only `ready` steps can be claimed. Repeating the claim with the same worker ID
renews the lease without incrementing `attempt`. Another worker receives
`409 ticket_claimed` until the lease expires; an expired lease can be reclaimed.

Progress body:

```json
{
  "worker_id": "pi-code-1",
  "event": "tests",
  "detail": "Focused tests pass."
}
```

Completion body:

```json
{
  "worker_id": "pi-code-1",
  "summary": "Implemented and verified the endpoint.",
  "artifacts": ["artifacts/result.md"],
  "dox_unchanged_reason": "No DOX contract changes."
}
```

Completion and blocking require the active owner plus either `dox_report` or
`dox_unchanged_reason`. Dependencies become `ready` only after their required
predecessors complete.

## Hermes

Copy [`integrations/hermes/imperium-work-pages`](../integrations/hermes/imperium-work-pages)
to `~/.hermes/skills/imperium-work-pages`, then restart Hermes. Set:

```text
IMPERIUM_BASE_URL=http://127.0.0.1:9000
IMPERIUM_API_KEY=<only when router auth is enabled>
```

The skill creates plans and reads status. It does not claim worker steps.

## Pi

Try the package directly:

```text
pi -e ./integrations/pi/imperium-work-pages
```

The package registers read, claim, log, complete, and block tools. Pi extensions
run with the user's permissions, so install only reviewed code.

## Deliberate limits

- No dispatcher or worker spawning.
- No dashboard page.
- No personal Hermes memory in worker tickets.
- No dependency on the separate `pnp` CLI; a future adapter may export these
  workspaces after that runtime is installed.

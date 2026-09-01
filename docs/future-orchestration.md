# Future orchestration

Advanced orchestration remains a product direction, but replacement work is
deferred until the Windows first-run installer, managed llama.cpp runtime,
model bootstrap, and local chat path are stable.

The observe-only `/orchestrator/*` plan and ticket API remains available for
existing clients in 0.8.0. It is authenticated, hidden from the dashboard, and
returns `Deprecation: true`. No removal date is currently assigned. The
router-backed Agent Library (`/agents`) is separate and is not deprecated.

The compatibility pilot now also supports atomic ticket claims with renewable
leases, owned progress logs, and owned complete/block actions. The bundled
Hermes skill and Pi extension exercise that boundary, but do not dispatch or
start workers. This does not satisfy or bypass the replacement re-entry gates.

## Direction

A future execution surface should provide visible workspaces, controllable
terminal processes, attention notifications, and an explicit CLI or socket
boundary. Imperium should continue to own model routing, hardware-aware
capacity, task policy, and safe audit data; an optional execution adapter
should own terminal processes and presentation.

The replacement must not make a platform-specific terminal application a
Windows runtime dependency.

## HIVE durable-task contract (post-gates)

Spynel's task/goal and recovery patterns are useful design evidence, not a
runtime dependency. The current adoption sequence is recorded in the
[Spynel adoption plan](spynel-adoption-plan.md). HIVE should adopt the
smallest durable contract after the re-entry gates pass:

- One Markdown task record with `objective`, `constraints`, `evidence`,
  `status`, and append-only progress.
- A first lifecycle of `todo → working → review → done/failed/waiting`.
- A minimal lease containing `owner`, `phase`, `heartbeat`, and `attempt`, but
  never a prompt body.
- A claim journal written before state mutation so restart recovery can
  distinguish an owned claim from work that was never dispatched.
- Executors call Imperium through its API or MCP boundary, never a model slot
  directly. ACP may be evaluated later as an optional executor adapter.

The central acceptance test is crash recovery: terminate HIVE after claim and
before completion, restart it, and prove that no task is dispatched twice and
every worker remains visible and stoppable. Do not copy Spynel implementation
code into Imperium. Any later material code reuse requires a fresh license
review and preservation of its MIT notice.

## Re-entry gates

- Windows first-run and full offline clean-room tests are green.
- The task/process boundary is specified independently of any terminal UI.
- Every worker is visible, stoppable, capacity-bounded, and tied to artifacts
  and verification results.
- Prompts and secrets remain absent from telemetry and summary endpoints.
- The orchestration package is optional; the router starts without it.
- A migration and removal policy exists for the deprecated API.

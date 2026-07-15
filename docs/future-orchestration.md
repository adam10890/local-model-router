# Future orchestration

Advanced orchestration remains a product direction, but replacement work is
deferred until the Windows first-run installer, managed llama.cpp runtime,
model bootstrap, and local chat path are stable.

The observe-only `/orchestrator/*` plan and ticket API remains available for
existing clients in 0.8.0. It is authenticated, hidden from the dashboard, and
returns `Deprecation: true`. No removal date is currently assigned. The
router-backed Agent Library (`/agents`) is separate and is not deprecated.

## Direction

A future execution surface should provide visible workspaces, controllable
terminal processes, attention notifications, and an explicit CLI or socket
boundary. Imperium should continue to own model routing, hardware-aware
capacity, task policy, and safe audit data; an optional execution adapter
should own terminal processes and presentation.

The replacement must not make a platform-specific terminal application a
Windows runtime dependency.

## Re-entry gates

- Windows first-run and full offline clean-room tests are green.
- The task/process boundary is specified independently of any terminal UI.
- Every worker is visible, stoppable, capacity-bounded, and tied to artifacts
  and verification results.
- Prompts and secrets remain absent from telemetry and summary endpoints.
- The orchestration package is optional; the router starts without it.
- A migration and removal policy exists for the deprecated API.

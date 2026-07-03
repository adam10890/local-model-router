# Added

- Dedicated, pinned OpenAI-compatible harness URLs and generated consumer
  setup for Hermes, Pi, Agent Zero, and optional Claude Code local mode.
- Canonical harness configuration with opt-in authenticated atomic writes.

# Changed

- The dashboard and documentation now distinguish harnesses from the agents
  and roles they manage internally.

# Fixed

- Harness clients can use the router without sharing the generic `/v1` route
  or connecting directly to model-server ports.

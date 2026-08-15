# 0001 - Hermes is the primary client

## Status

Accepted

## Context

Imperium serves many clients (Hermes, Agent Zero, Pi, Cursor, Claude Code).
Maintenance time is limited. Hermes currently learns the operator better than
the others.

## Decision

Hermes is the primary client for Imperium support and integration quality.
Other clients stay supported as OpenAI-compatible harness users, not as equal
first-class product focus.

A dedicated Imperium component must exist to pin and enforce rules about
client priority, routing policy, and compute use — so preference for Hermes
is not only tribal knowledge.

## Consequences

- Hermes harness path, docs, and live smoke get first attention.
- Agent Zero and Pi remain clients, not Imperium components.
- Future policy/rules surface is required; ad-hoc YAML alone is not enough.

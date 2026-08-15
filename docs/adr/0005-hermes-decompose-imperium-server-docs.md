# 0005 - Task split: prefer Hermes layer, document Imperium for servers

## Status

Accepted

## Context

Sub-task decomposition can live in the client or in the router. Hermes may
already provide that layer. The operator also wants Imperium installable as
a standalone system on a cloud server.

## Decision

If Hermes already owns task decomposition, use that layer for the daily
Hermes workflow. Do not duplicate a second orchestrator inside Imperium for
that path.

Separately, document an Imperium-owned infrastructure plan (routing,
capacity, policy, optional future execution adapter) so Imperium can be
installed as a standalone compute gateway on a cloud server without Hermes.

Daily orchestration replacement code stays gated by
`docs/future-orchestration.md`.

## Consequences

- Docs must describe standalone server install of Imperium as a gateway.
- Hermes remains primary interactive client on the operator workstation.
- Imperium does not become Hermes; it remains the compute router.

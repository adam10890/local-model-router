# 0004 - Warm weak model, seamless promote to strong

## Status

Accepted (direction)

## Context

Hermes stays running as a client. The GPU need not hold the strong model
all day. The operator accepts a short load delay if quality then feels
instant.

## Decision

While idle or lightly waiting, Imperium may keep a weaker model loaded (or
cold/hot per llama.cpp policy). When interactive quality is needed, promote
to the strong model. Target: the operator should not feel the handoff.

About five seconds of promote/load latency is the UX budget (ADR 0009).
Permanent GPU residency of the strong model is not required.

## Consequences

- Hot/cold or dual-slot policies are in scope for compute maximization.
- Routing must explain promote/demote with reason codes.
- Latency budgets belong in readiness/status, not silent stalls.

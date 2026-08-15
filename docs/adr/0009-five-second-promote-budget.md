# 0009 - Strong-model promote budget is five seconds

## Status

Accepted

## Context

Idle may use a weak warm model. Interactive chat should promote to a strong
model without the operator “feeling” the handoff (ADR 0004).

## Decision

The promote/load budget for the strong model is **about five seconds**.

Longer stalls are a product failure for interactive Hermes chat unless the
operator explicitly chose a cold path.

## Consequences

- Hot/cold and dual-slot design must meet ~5s promote for the common case.
- Status/readiness should surface when promote will exceed that budget.
- Quality still beats speed when they conflict (ADR 0006), but promote
  latency has a hard UX target.

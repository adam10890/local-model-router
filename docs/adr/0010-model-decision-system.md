# 0010 - Need an explicit model decision system

## Status

Accepted (direction)

## Context

Today Hermes is pinned to one Imperium model. Nobody chooses weak-vs-strong
per turn. Automatic maximize-compute (ADR 0002/0004) needs a decision layer.

## Decision

Plan an Imperium **model decision system** that can choose (or recommend)
weak vs strong (and later loop/catalog roles) with explainable reasons.

Until that exists, Hermes stays on one pinned model. Do not pretend per-turn
choice already happens in the client.

## Consequences

- Docs and design for decision policy come before fake “auto quality” UX.
- Harness pins remain authoritative for Hermes until the decision system can
  safely vary models without silent pin breaks.
- Decision output must stay explainable (reason codes) and local-first.

# 0010 - Need an explicit model decision system

## Status

Accepted (direction)

## Context

This decision originally recorded Hermes as pinned to one Imperium model and
no weak-vs-strong choice per turn. Automatic maximize-compute (ADR 0002/0004)
still needs a decision layer. ADR 0015 later added an explicit Hermes
session-selected swap between named GPU profiles; it is not general automatic
quality routing.

## Decision

Plan an Imperium **model decision system** that can choose (or recommend)
weak vs strong (and later loop/catalog roles) with explainable reasons.

Until that system exists, do not present automatic per-turn weak-vs-strong
choice. Hermes may request the explicit profile names defined by ADR 0015, and
Imperium updates its connection pin only after the requested GGUF is verified
healthy.

## Consequences

- Docs and design for decision policy come before fake “auto quality” UX.
- The Hermes connection pin remains authoritative for serving requests. ADR
  0015 may update it in memory after a verified session-selected load, without
  silently serving a restored or unrelated model.
- Decision output must stay explainable (reason codes) and local-first.

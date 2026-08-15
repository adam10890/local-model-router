# 0011 - Docs order: hot/cold policy, then loop catalog

## Status

Accepted

## Context

Before more code, the founder wants documentation for compute policy. Two
candidates were hot/cold policy and a predefined agentic-loop catalog.

## Decision

Write docs in this order:

1. Hot/cold (warm weak → promote strong) policy.
2. Then the predefined agentic-loop catalog.

Code for those features follows the docs, subject to beta gates and
orchestration re-entry rules.

## Consequences

- Next brain-to-docs / planning work starts with hot/cold policy text.
- Loop catalog is second, not parallel, unless the founder reorders.

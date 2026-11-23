# 0011 - Docs order: hot/cold policy, then loop catalog

## Status

Accepted

## Context

Before more code, the founder wants documentation for compute policy. Two
candidates were hot/cold policy and a predefined agentic-loop catalog.

## Decision

We will write the hot/cold policy before the agentic-loop catalog, and we will not start that code ahead of the docs.

Write docs in this order:

1. Hot/cold (warm weak → promote strong) policy.
2. Then the predefined agentic-loop catalog.

Code for those features follows the docs, subject to beta gates and
orchestration re-entry rules.

## Consequences

- Next brain-to-docs / planning work starts with hot/cold policy text.
- Loop catalog is second, not parallel, unless the founder reorders.

Negative: the loop catalog waits, even when a loop question is ready, until hot/cold is dealt with or the founder reorders.

# 0012 - Hot/cold policy doc started

## Status

Accepted

## Context

ADR 0011 ordered docs as hot/cold policy first, then the loop catalog. The
founder asked to start writing.

## Decision

We will keep the hot/cold design in `docs/hot-cold-policy.md`, and we will not describe that design as shipped behavior.

The living design doc is [`docs/hot-cold-policy.md`](../hot-cold-policy.md).
It records accepted direction and open founder questions. It does not claim
the behavior is already shipped.

## Consequences

- Further hot/cold design edits land in that doc (or new ADRs when a choice
  hardens).
- Loop-catalog doc waits until hot/cold open questions are answered or the
  founder reorders.

Negative: the doc can move ahead of the code, and a reader can mistake accepted direction for behavior the router already has.

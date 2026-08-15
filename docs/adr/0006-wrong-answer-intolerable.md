# 0006 - Wrong answer is the intolerable daily failure

## Status

Accepted

## Context

Daily failures include slow replies, wrong replies, and the strong model
dropping mid-session. Priorities must be explicit.

## Decision

The intolerable daily failure is a **wrong answer**.

Prefer correct quality over raw speed when they conflict. Model dropouts and
slowness matter, but incorrect output is the primary pain to prevent.

## Consequences

- Routing and promote-to-strong policies bias toward quality for chat.
- Parallel small models must not silently lower correctness on critical work.
- Explainability and evaluation hooks matter more than shaving seconds.

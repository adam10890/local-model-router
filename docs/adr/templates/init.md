# 0001. Record architecture decisions

Date: {{DATE}}

## Status

Accepted {{DATE}}

## Context

We need to record the architectural decisions made on this project so that current and
future contributors, human or agent, can understand why the system is built the way it is.
Without a durable record, decision rationale is lost between sessions and handoffs.

## Decision

We will use Architecture Decision Records, as described by Michael Nygard
(https://www.cognitect.com/blog/2011/11/15/documenting-architecture-decisions), stored as
Markdown files in this directory, numbered monotonically, one decision per file. Accepted and
rejected records are immutable; changes are made by superseding.

## Consequences

Positive: decisions and their trade-offs are discoverable next to the code and reviewable in PRs.
Negative: authors must spend time writing and reviewing records; the log needs upkeep.
Neutral: CI runs the ADR linter on every change to this directory.

## Change History

| Version | Date | Author | Change |
|---|---|---|---|
| 1.0 | {{DATE}} | {{OWNER}} | Bootstrap decision log |

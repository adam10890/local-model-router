# 0017. Task ownership: commitments for intent, executors for execution state

Date: 2026-11-23

## Status

Accepted 2026-11-23 (v1.0)

## Context

The idea queue carried two open questions that blocked work. Open question 1: `future-orchestration.md` said Imperium continues to own task policy, while `spynel-adoption-plan.md` said HIVE, not Imperium, owns durable task orchestration. Open question 2: the Herdr packages and the Q7 Hermes-during-Studio brief each needed a canonical task service, and no document named one.

There were four candidates. מעקב אדם (Adam's task tracker) is already the SharedBrain source of truth for tasks Adam asks to hand to agents. It provides idempotent `create-commitment`, a `blocked` status, and an append-only event log. Hermes Kanban is installed and has dependency checks, atomic worker claims, and chat-scoped notification subscriptions, but its intake can admit concurrent duplicates and it counts `archived` parents as satisfied. HIVE is a post-gate proposal with no implementation. Imperium is the model router.

On 2026-11-23 Adam approved the review's recommendation in chat. He noted that Hermes was built to manage tasks and may own their execution.

The decision can be reversed until a second executor writes execution state, after which moving records means a migration.

## Decision

We will keep what Adam asked to have done as a commitment in מעקב אדם, keep execution state with the executor that runs the work, and keep Imperium out of task ownership.

- Intent: every request Adam makes to have something done, including "after this, do X", reminders, and follow-ups, is a מעקב אדם commitment created through its service or CLI.
- Execution: the executor that runs the work owns its attempts, claims, dependencies between attempts, and retries. For work Hermes runs, that is Hermes Kanban. Every execution record links to its commitment ID, and execution never marks a commitment complete without its required evidence.
- Imperium owns inference routing, admission, and GPU ownership. It does not store tasks or task policy.
- HIVE stays a proposal. It returns for a decision only when two or more executors must coordinate one task.

This answers open questions 1 and 2 in `docs/future-improvements.md`.

Rejected: Hermes Kanban as the user-facing authority, because it would create a second task list beside מעקב אדם. Imperium as the task service, because routing and task policy would then share one failure domain and the router is lifecycle-free by default. Building HIVE now, because no second executor needs it yet.

## Consequences

Positive: Adam sees every requested task in one place. Executors keep the state they need to run work reliably, and the router stays a router. Q7's task store no longer waits on an ownership decision.

Negative: Hermes needs an adapter that creates and links commitments, and the link can drift if either side fails mid-write. That needs idempotent keys and a reconciliation check. `future-orchestration.md` loses its claim that Imperium owns task policy. An executor without a commitment link is non-compliant until it gains one.

Neutral: Hermes Kanban still needs transactional intake and an explicit success check on dependencies before auto-dispatch, as the Q7 brief requires. The daily sync between מעקב אדם and another kanban (`k000`) must not duplicate commitments that Hermes creates.

Compliance: a PRD or design that stores user-requested work names the commitment link. A review rejects a new user-facing task store.

Review trigger: a second executor needs to coordinate the same task, or מעקב אדם cannot serve an agent-facing API.

## Change History

| Version | Date | Author | Change |
|---|---|---|---|
| 0.1 | 2026-11-23 | claude | Initial proposal, approved by Adam in chat the same day |
| 1.0 | 2026-11-23 | agent | Accepted |

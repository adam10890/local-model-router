# 0019. Acknowledge Studio jobs before acquiring the GPU lease

Date: 2026-11-23

## Status

Proposed

## Context

An accepted Studio submission can begin borrowing the GPU before Hermes's
post-tool turn generates the launch acknowledgment. The [Q4 hermetic
baseline](../1.0-beta-evidence.md#hermes-during-studio-baseline-g2) exercises
that order with a simulated lease and retry policy. Adam wants one start
message with an ETA or “unknown,” followed by the result or an actionable
failure. The GPU can be unavailable to the strong model for that interval.

[ADR 0015](0015-hermes-gpu-session-swap.md) governs the GPU handoff. Under
[ADR 0017](0017-task-ownership-commitments-for-intent-executors-for-execution-state.md)
and `commons::[[concepts/personal-dashboard-task-source-of-truth]]`, user
intent lives in מעקב אדם. Hermes Kanban owns the linked execution state, and
Hermes authorizes delivery to the originating conversation. Imperium admits
GPU use.

## Decision

We will have Studio send one deterministic start message through its existing
authorized delivery path after durable submission acceptance and before
attempting to acquire the GPU lease. Use the stable job ID and a measured ETA
or “unknown.” The message describes an accepted job that is starting. Track
lease acquisition separately. Hermes ends the launch turn from the accepted
submission without requiring a second model completion for the acknowledgment.

Deduplicate the start action by job ID across render retries and submission
replays. Require a delivery-path receipt before the lease attempt. Preserve
uncertain sends for reconciliation. Report a rejected lease or failed delivery
through the approved failure path. Keep job and delivery ownership attached to
the originating conversation.

A post-tool model acknowledgment depends on inference during the lease.
Sending a start message only after lease acquisition ties the acknowledgment
to the duration of drain and acquisition.

## Consequences

The user gets a single start acknowledgment without a model inference call
after lease acquisition. The start message and the final result can use the
same job identity while reporting distinct states.

The trade-off is that a delivery-path outage can delay a render before it
starts. A receipt confirms delivery-path acceptance. Preserve the accepted job
for recovery and verify the origin and the message order with an approved
WhatsApp canary later.

Use fake delivery and fake leasing to verify that the receipt precedes the
first lease request, immediate claim cannot strand the launch turn, and a
duplicate submission or render retry emits one start message. Exercise lease
rejection and ambiguous delivery. Revisit the gate if the delivery path cannot
give a durable receipt that supports reconciliation.

## Change History

| Version | Date | Author | Change |
|---|---|---|---|
| 0.1 | 2026-11-23 | codex | Initial proposal |

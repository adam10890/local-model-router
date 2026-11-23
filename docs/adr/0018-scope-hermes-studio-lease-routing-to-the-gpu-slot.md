# 0018. Scope Hermes Studio lease routing to the GPU slot

Date: 2026-11-23

## Status

Proposed

## Context

The [Q4 hermetic baseline](../1.0-beta-evidence.md#hermes-during-studio-baseline-g2)
shows a named CPU connection reaches its pinned slot until Studio borrows the
GPU. After lease acquisition, the early Hermes ingress gate returns
`503 gpu_leased_by_studio` for both connections before forwarding. The existing
post-admission check is slot-scoped. It runs after route selection and can be
unreachable for a stopped GPU slot. An early lease response is still needed
for the GPU path.

[`HarnessProfiles.resolve`](../../local_model_router/harnesses/profiles.py)
requires a connection name when there are multiple connections. The harness
contract in [`AGENTS.md`](../../AGENTS.md) currently names Agent Zero as the
multi-connection exception. Adding a Hermes CPU connection requires an
explicit default for existing unnamed Hermes requests and approval of the
contract change.

[ADR 0004](0004-warm-weak-seamless-strong.md) sets the warm standby direction.
[ADR 0006](0006-wrong-answer-intolerable.md) prioritizes correctness and
[ADR 0009](0009-five-second-promote-budget.md) bounds strong promotion.
[ADR 0010](0010-model-decision-system.md) requires explainable model selection.
[ADR 0015](0015-hermes-gpu-session-swap.md) keeps the strong connection pinned
to a verified GPU profile. Under
[ADR 0017](0017-task-ownership-commitments-for-intent-executors-for-execution-state.md),
Hermes owns eligibility and execution. Imperium owns inference admission and
GPU ownership. User intent is a commitment in מעקב אדם under the SharedBrain
decision `commons::[[concepts/personal-dashboard-task-source-of-truth]]`.

## Decision

We will scope the early Hermes watcher and lease rejection to the managed GPU
connection, using its effective pin and slot identity before health-based
selection. Keep verified GPU profile refresh and pin publication on that path.
Keep the existing post-admission slot check so a lease acquired during request
admission is still enforced.

Add an opt-in named CPU standby connection for Hermes with its own verified
local pin. Keep the unnamed Hermes path bound to `default` when the standby is
configured. Update the harness contract for this Hermes exception if this
proposal is accepted. Retain the existing resolver behavior for other
harnesses.

Keep the leased GPU response `503 gpu_leased_by_studio` and `Retry-After` for
existing callers. A Hermes client with standby support will consume that
specific code before generic retry. Application code may send an eligible
request to the verified CPU standby once. Eligibility requires a registered
skill or loop, or an allowlisted read-only tool, with no planning phase. Other
work waits for the strong path. Typed commitment intake uses an authorized
write path. The decision is explicit, local-only, and subject to the existing
[task-disclosure policy](../task-disclosure.md).

A separate `hermes_standby` identity would preserve one default connection per
harness and avoid a resolver exception. It would add another identity to
configure and verify, and leave the named-connection regression unresolved.
Removing `Retry-After` would change behavior for existing clients. The
existing error code carries the distinction the upgraded client needs.

## Consequences

The same harness can reach a distinct local CPU slot during a GPU lease. The
strong pin and legacy unnamed connection keep their verified routing.

The compatibility cost is a deliberate Hermes exception and a coordinated
client change. An admitted CPU slot also needs measured Windows headroom.
Disable the standby configuration to roll back the client behavior.

Verify the existing unnamed URL, the named CPU regression, the strong lease
response, a lease acquired during admission, and strong profile refresh after
release. Verify that an ineligible turn takes the strong wait path and that a
failed standby does not replay a tool call or widen disclosure. Revisit this
decision if the compatibility exception expands to another harness or the CPU
slot cannot meet the measured responsiveness budget.

## Change History

| Version | Date | Author | Change |
|---|---|---|---|
| 0.1 | 2026-11-23 | codex | Initial proposal |

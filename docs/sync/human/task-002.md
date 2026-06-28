# Task 002 — Proposal: Tiered Context Budget as a routing signal

> Proposal only — no behavior change in this document. Reference architecture:
> Agent Zero's `context_analyzer` (utilization tiers) and `is_over_limit()`.

## Context (grounded in current code)

The router already carries the Agent-Zero-inspired math:

- `helpers/context_planner.py` — `DEFAULT_EFFECTIVE_CTX_RATIO = 0.70` (the same
  ratio as Agent Zero's `chat_model_ctx_history`), VRAM-aware max-feasible
  planning, and an effective budget feeding the "compression guard".
- `helpers/context_calculator.py` — `ExternalTokenBudget` (pen_paper, wiki,
  history, system, `reserve_response`) and `recommend_context_for_budget`.

The gap: this is **internal planning only**. It is neither tiered nor exposed as
an explainable routing decision.

## Proposed system (small, behavior-preserving)

1. **Tiered utilization**: extend the budget to return a zone
   (green ≤50% / yellow ≤70% / orange ≤85% / red) alongside the raw number,
   mirroring `context_analyzer`.
2. **Context-fit as a reason code**: when a request will not fit a slot's
   window, emit `reason: "context_overflow"` and recommend an alias with a
   larger window — satisfying AGENTS.md's "routing must stay explainable, never
   silent fallbacks".
3. **Surface it**: add a `context` block (window, used, zone) to the observer /
   `GET /v1/models` output and the dashboard.

## Integration points

`helpers/context_calculator.py` (tier helper) and
`service/routing_intent.py` (reason code), with read-only surfacing in the
observer/dashboard. No change to loopback defaults, auth, or fleet behavior.

## Constraints honored (AGENTS.md / CLAUDE.md)

- Keep increments small and behavior-preserving; stdlib/native first.
- No Agent Zero imports; the router stays the product.
- Explainable routing; redact secrets; never include prompt bodies.

## Status / priority

In progress · medium (infrastructure already exists; this is exposure + tiers).

**Implemented (this branch):** `helpers/context_calculator.py` gains
`utilization_zone()` + `ContextUtilization` (green/yellow/orange/red,
`EFFECTIVE_CTX_RATIO = 0.70`), with tests in
`tests/test_context_calculator_tiers.py`. `service/routing_intent.py` now
appends an explainable `context_zone:<zone>` reason code and a
`context_overflow` reason + warning when the estimated request exceeds the
selected slot's effective budget — additive only, slot selection is unchanged.
Follow-up: surface the `context` block in observer / `GET /v1/models` / dashboard.

## Acceptance metrics

- Routing decisions that overflow a window carry `context_overflow` + a larger-window recommendation.
- Observer/models output reports a context zone per candidate slot.

# 0002 - Maximize local compute by role and parallelism

## Status

Accepted (direction). Implementation gated by beta stability and
`docs/future-orchestration.md`.

## Context

The operator wants Imperium to use available local compute automatically.
Idle time should not waste a strong GPU model. Chat needs quality. Some
sub-tasks look small enough for weaker models in parallel.

## Decision

Imperium should maximize useful local compute automatically:

1. **Idle / listen** — keep a weak, cheap model warm (or a hot/cold policy
   via llama.cpp) so something is ready without holding the big model.
2. **Interactive chat** — prefer a strong model for answer quality.
3. **Predefined agentic loops** — catalog fixed sub-agent task loops with an
   explicit model (or role) per loop for best quality/cost fit.
4. **Parallel small work** — when a task is precise and bounded, run it on
   smaller models across multiple terminals/slots to cut wall-clock time
   versus always using the largest model.

Imperium owns routing, capacity, and policy. Clients (Hermes and others)
own their agent loops unless an optional execution adapter is later added
under the orchestration re-entry gates.

## Consequences

- Fleet control, hot/cold or weak standby slots, and role-aware routing are
  in scope for this vision.
- Always-on max model for every request is rejected as default policy.
- Full multi-terminal orchestration stays deferred until gates in
  `docs/future-orchestration.md` pass.
- Policy must stay explainable (reason codes), local-first, and free of
  prompt bodies in telemetry.

## Devil's advocate (recorded assumptions to test)

- Small models are not always “precise task” safe; wrong split can multiply
  failures and retries.
- Parallel slots still share one GPU/VRAM budget; more terminals can thrash
  the strong chat model.
- Hot/cold switching has load latency; “always listening” may still cost
  VRAM or swap time.
- Predefined loops can rot; without evaluation, the catalog becomes fiction.
- Hermes already owns agent loops; duplicating that inside Imperium risks
  two orchestrators. Prefer Imperium as router + policy first.

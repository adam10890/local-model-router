# Hot / cold compute policy

**Status:** Accepted direction (not shipped behavior yet)  
**Owner:** Product / fleet routing  
**Decisions:** ADR 0002, 0004, 0006, 0009, 0010, 0011, 0015
**Next doc:** predefined agentic-loop catalog (after this policy stabilizes)

This document is the operator design for Imperium’s warm-weak / promote-strong
behavior. It is **not** a claim that the router already does this. ADR 0015
ships a separate, explicit Hermes session choice between large GPU profiles;
that one-card cold swap is not automatic weak-vs-strong routing.

## Goal

Use local GPU compute well:

- Do not keep the strongest model on the GPU all day for no reason.
- Keep something ready so Hermes does not feel “dead” while idle.
- When quality matters, promote to the strong model within about **five
  seconds** (ADR 0009).
- Prefer a **correct** answer over a fast wrong one (ADR 0006).

## Roles

| Role | Intent | Typical state |
| --- | --- | --- |
| **Cold** | Not loaded. Saves VRAM. | Slot stopped or model unloaded. |
| **Warm weak** | Cheap model loaded and healthy. Answers light traffic or waits. | Default idle posture. |
| **Hot strong** | Quality model loaded for interactive chat. | After promote for a quality request. |

Exact model ids for weak/strong are **machine-local** and stay out of this
doc. Configure them in the local fleet / decision policy, not in git.

## States

```text
        idle / listen
              |
              v
        [warm weak] ----promote (quality need)---> [hot strong]
              ^                                         |
              |---------demote (idle long enough)-------|
              |
         (optional) cold weak if VRAM needed elsewhere
```

### Warm weak

- At least one weak chat-capable slot is running and healthy when Imperium
  intends to “listen.”
- Used for readiness probes, tiny checks, or temporary replies only if the
  decision system allows it without violating quality rules.
- Must not silently replace a Hermes quality pin with a weak model.

### Promote to hot strong

- Trigger: interactive quality need (chat / Hermes quality path) decided by
  the future **model decision system** (ADR 0010).
- Budget: load or swap so the strong model can serve within ~**5 seconds**.
- If promote will miss the budget, surface that in status/readiness — no
  silent multi-minute hang.
- Explain with reason codes (for example `promote_strong`, `warm_weak_ready`,
  `promote_budget_exceeded`).

### Demote

- After idle long enough (exact timeout TBD), unload or stop the strong slot
  and return to warm weak if VRAM should be freed.
- Demote must not drop an in-flight request.

## Relation to Hermes pins

The Hermes connection pin stays **authoritative** for serving requests. ADR
0015 may update it in process memory only after a session-selected profile is
loaded and verified; desired, pin, and loaded GGUF remain separate status.

Until then:

1. An operator may explicitly select one of the approved Hermes profiles.
2. Hot/cold automation must not pretend automatic per-turn choice exists.
3. Dual-slot experiments (weak + strong) remain separate from the serialized
   one-card session swap.

## Failure preference

| Event | Preference |
| --- | --- |
| Wrong answer from weak model on a quality chat | Unacceptable (ADR 0006). |
| ~5s wait then strong answer | Acceptable. |
| Promote fails / exceeds budget | Explicit error or status; do not fake success on weak unless policy says so (open — see below). |
| Strong model OOM while weak is warm | Fail explainably; keep weak for recovery if safe. |

## Open decisions (founder)

These are not invented here; answer later via brain-to-docs / ADR:

1. Which GGUFs are warm-weak and hot-strong on this machine?
2. After a failed 5s promote: wait longer, answer with weak, or hard error?
3. May promote happen mid-Hermes conversation, or only on a new request?
4. Idle time before demote?
5. What the rules component locks first (client→model, VRAM, cloud)?

## Implementation sketch (deferred code)

Non-binding shape for later work; gated by beta stability and approval
(ADR 0007):

1. Fleet: two chat slots or one router-mode slot with fast model swap.
2. Decision system: inputs = harness/app, task hints, VRAM headroom, health.
3. Outputs = selected slot/model + reason codes + promote ETA.
4. Dashboard/status: show warm/hot/cold and promote budget risk.
5. Never log prompt bodies.

Orchestration of many terminals stays under
[`future-orchestration.md`](future-orchestration.md).

## Verification (when implemented)

- Idle: weak warm, strong not holding full VRAM (or documented exception).
- Quality request: strong serves with promote ≤ ~5s on the reference NVIDIA
  box, or status shows budget miss.
- Hermes pin: no silent model change; ADR 0015 changes it only after a verified
  explicit profile load.
- Reason codes present on promote/demote paths.
- Wrong-answer risk: weak must not answer quality Hermes chat unless
  explicitly allowed by policy.

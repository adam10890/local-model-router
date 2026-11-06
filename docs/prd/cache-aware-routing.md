# PRD — Cache-aware routing

**Status:** Proposed — not shipped behavior
**Owner:** Product / founder
**Goals served:** G3 (explainable routing), G6 (daily use that feels responsive)
**Decisions:** ADR 0002 (maximize local compute), ADR 0009 (five-second promote budget)
**Gate:** deferred until the 1.0 beta gates in [`../1.0-beta-evidence.md`](../1.0-beta-evidence.md) pass
**Catalogue origin:** `LMCache`, and the prefix-cache-aware scheduling in `sglang` and `dynamo`

## Problem

Two caches exist and neither helps a conversation.

`service/prompt_cache.py` is opt-in, in-memory, and returns a stored reply only
for a byte-identical repeat of a deterministic request. A multi-turn agent
session never qualifies: the system prompt and the first eight turns are
identical, the ninth message is new, so the key differs and it misses every
time.

Underneath, llama.cpp keeps that shared prefix warm in its own KV cache — but
in **one specific slot**, the one that served the previous turn. Ranking in
`routing/catalog.py:rank_candidates` has no notion of this. Turn two is scored
on role, health, context fit, latency hint and locality, and can land on a cold
slot that re-evaluates the entire prompt from scratch.

For a long-context agent conversation, prompt evaluation dominates the
response. Paying it again because the router forgot where the conversation was
is waste the router is uniquely positioned to avoid — and it is the kind of
waste a user experiences as the product being slow, which is G6's concern.

## Scope

**Fingerprint the prefix, not the request.** Hash the stable leading portion of
the message list — system prompt plus all turns except the newest — into a
prefix id. The whole-request hash in `prompt_cache.cache_key` stays as it is
for the reply cache; this is a second, coarser key whose job is to answer
"which slot has probably seen this before".

**Record where it was served.** A bounded, in-memory map of prefix id to slot
id with a short TTL, in the same spirit as the existing health-probe TTL cache
in `helpers/smart_router/health.py`. It is a hint, not state: losing it costs a
slow turn, never a wrong answer.

**A bounded bonus inside the existing ranking.** Add an affinity term to
`rank_candidates` so a warm slot wins among candidates that are otherwise
close. The bonus must be small enough that it never outranks health, a
capability rejection, a context-window rejection, or an admission decision — a
warm slot that cannot serve the request is still the wrong slot. It breaks
ties; it does not override policy.

**Say so.** Reason codes, in the established style:

| Code | Meaning |
| --- | --- |
| `cache_affinity_hit` | a warm slot was known and selected |
| `cache_affinity_miss` | no slot was known for this prefix |
| `cache_affinity_ignored_unhealthy` | a warm slot was known but rejected on health or capability |
| `cache_affinity_ignored_admission` | a warm slot was known but its lane was full |

The last two matter most: they are what distinguishes "the optimization did not
apply" from "the optimization is broken", and without them a disappointing
latency has no explanation.

**Measure it.** A hit rate in the metrics from
[`observability.md`](observability.md). An affinity feature whose effect cannot
be observed should not ship.

## Non-goals

- Moving or copying KV blocks between slots. That is `LMCache`'s problem and it
  needs backend support Imperium does not have.
- Pinning a conversation to a slot. Affinity is a preference; a pinned harness
  connection is a separate, stronger contract that already exists in
  `harnesses/`.
- Persisting the affinity map across restarts. A cold start costs one slow
  turn.
- Any change to the existing reply cache's correctness rules. It stays
  deterministic-only and opt-in.
- Overriding `local_only`, disclosure caps, or budget decisions. Affinity sits
  below all of them.

## Seams it reuses

- `local_model_router/routing/catalog.py` — `rank_candidates` and its
  `score_inputs`, which already carry per-factor contributions for
  explainability.
- `local_model_router/service/prompt_cache.py` — `cache_key` and the bounded
  TTL map pattern.
- `local_model_router/helpers/smart_router/health.py` — the TTL cache added in
  0.6.0, the closest existing precedent for a short-lived routing hint.
- `local_model_router/service/routing_intent.py` — reason codes and
  `ranked_candidates`.

## Open decisions

1. How large is the bonus? It has to beat a small latency-hint difference and
   lose to any health or capability signal. A number falls out of measurement,
   not from taste — which argues for building `/metrics` first.
2. What TTL? Long enough to span a user's thinking pause, short enough that a
   swapped model does not inherit a stale hint. A model swap must invalidate
   entries for that slot, which ADR 0015's swap path can signal.
3. Does affinity apply to upstreams, or only to local slots? An upstream's
   cache behavior is unobservable, so the honest default is local-only.
4. Should a warm-but-busy slot be waited for briefly instead of passed over?
   That trades the five-second promote budget of ADR 0009 against prompt-eval
   cost, and it is a product call.

## Verification

- Hermetic ranking tests: identical candidates where one is warm selects the
  warm one; a warm-but-unhealthy candidate is rejected and reports
  `cache_affinity_ignored_unhealthy`; a warm candidate that fails a capability
  or context check is still rejected.
- The bonus cannot invert a health, capability or `local_only` decision — a
  matrix test, in the style of the disclosure matrix in
  `tests/test_disclosure_trust.py`.
- Prefix fingerprinting is stable across turns and changes when the system
  prompt changes.
- The fingerprint is a hash and no message text reaches telemetry (G4).
- Live: a multi-turn conversation against a two-slot fleet, measuring
  time-to-first-token on turn two with affinity off and on. Without a
  measured difference the feature has not earned its place.

# PRD — Structured output contract

**Status:** Shipped in 0.14.0, observe by default — enforcement is opt-in
**Owner:** Product / founder
**Goals served:** G2 (client compatibility), G3 (explainable routing)
**Decisions:** ADR 0006 (a wrong answer is the intolerable daily failure)
**Gate:** deferred until the 1.0 beta gates in [`../1.0-beta-evidence.md`](../1.0-beta-evidence.md) pass
**Catalogue origin:** `outlines`, and the JSON/FIM support advertised by Granite 4.0 and Qwen3

## Problem

A client asks for JSON the OpenAI way, with `response_format`. Imperium reads
that field in exactly two places: `routing/catalog.py:170`, to decide whether a
candidate slot needs a JSON-mode capability, and `service/prompt_cache.py`, to
key the cache. The forward path at `service/app.py:465` copies it through
unchanged.

A slot that does not honor `response_format` returns prose instead. Nothing
downstream notices: Imperium does not parse the reply, so the client receives a
200 with a body it cannot load. For a tool-calling harness that is a wrong
answer dressed as a success, which ADR 0006 names the one failure the product
must not ship.

### Correction to this PRD's first draft

The first draft of this document said the fix was to translate
`response_format` into each backend's native dialect — llama.cpp `grammar`,
Ollama `format`, vLLM `guided_json` — "from a declared capability on the
target… slots declare theirs in the fleet YAML". Implementation found both
halves of that wrong, and the claim is corrected here rather than left
standing:

- **No dialect declaration exists.** `ModelCandidate.backend_type`
  (`routing/catalog.py:51`) is filled by `service/observer.py:146` from the
  global `backend` setting, so its value is `auto` / `subprocess` / `docker` /
  `remote` — a launch mechanism. Upstream `type` is only `openai_compatible`
  or `subscription`. Nothing tells the router which server it is talking to.
- **And no translation is needed.** Imperium only ever addresses
  OpenAI-compatible `/v1` surfaces: `observer.py:168` builds
  `http://host:port/v1` for every slot, and upstreams are `openai_compatible`
  by definition. The dialect divergence is between those servers' *native*
  APIs, which Imperium never calls.

What survived is the half that was real, and it is the half that matters:
nobody checks the answer.

## What shipped

**Check the answer.** `local_model_router/structured_output.py` is stdlib-only
and pure. When the forwarded payload carries `response_format`, the reply's
content is parsed with `json.loads`; a markdown code fence is unwrapped first,
because that is the common near-miss and rejecting it would be pedantry rather
than safety. A reply with nothing to inspect — a tool-call-only message, an
empty `choices` list — passes: the module judges only content it was given.

**Well-formedness, not conformance.** The founder's decision. A reply can parse
here and still violate the schema it was asked for. That is the known limit,
chosen over adding a JSON Schema validator as a core dependency (G7).
`jsonschema` happens to be importable in some environments as a transitive
dependency; nothing here relies on that.

**One bounded repair, when enforcing.** A single re-POST to the same target
with one appended system message naming the parser error — never the failed
output, which the model already has in context. It rides the admission lease
the request already holds: a second acquire would deadlock at `max_active=1`,
the configuration the ADR 0015 soak run used.

**Observe by default.** The verdict rides in `x-a0-router-structured-output`
and in telemetry; the body is returned exactly as the model sent it.
`A0_LMM_ROUTER_STRUCTURED_OUTPUT=1` enables repair-then-refuse. Enforcement
changes a client-visible contract — a 422 where a harness expects 200 — so it
is an explicit, reversible opt-in, read per request like the disclosure gate.
Six of seven beta goals are Partial; turning this on is a decision for after
they close.

**A failed reply is never cached.** The prompt cache is written from the same
object that is returned (`app.py`, local non-streaming path). Caching a prose
reply would serve it again as a hit and skip the check entirely, making the
failure permanent and invisible.

**Reason codes.**

| Code | Meaning |
| --- | --- |
| `structured_output_valid` | JSON was requested and the reply parsed |
| `structured_output_repaired` | the first reply failed, the retry parsed |
| `structured_output_failed` | both attempts failed; 422 when enforcing |
| `structured_output_unsupported` | the request was streamed, so no check was possible |

## Non-goals

- Translating to a native backend dialect. See the correction above: there is
  nothing to translate to on the path Imperium uses.
- Schema conformance checking, and any new runtime dependency for it.
- Repairing more than once, or silently downgrading a schema request to prose.
- Streaming validation. `_stream_upstream_response` never buffers, and
  buffering to validate would change the latency contract. A streamed JSON
  request reports `structured_output_unsupported`.

## Seams it reuses

- `local_model_router/service/app.py` — the local non-streaming response block
  and `forward_to_upstream`, hooked after the body is parsed and before it is
  cached or returned.
- `local_model_router/routing/catalog.py` — `_has_json_mode`. A test asserts
  the two predicates agree, so a request cannot be routed on one rule and
  checked on another.
- The `_disclosure_enforced()` pattern for the opt-in flag, and the
  `finish(...)` telemetry call for the reason code.

## Decisions taken

1. **Validation depth:** well-formedness only, stdlib. No new dependency.
2. **Double failure:** an explicit 422, not a 200 carrying prose. ADR 0006
   prefers a visible failure to a success that is not one.
3. **Admission lease on repair:** rides the original. Mine, not the founder's,
   for the deadlock reason above.

## Still open

- Whether a slot that cannot honor `response_format` should be ranked below
  one that can. Routing already rejects candidates lacking `json_mode` when
  the request needs it, so this only matters once a slot declares the
  capability and then does not deliver.
- Whether enforcement should default on once the beta gates close.

## Verification

Covered by `tests/test_structured_output.py` (pure functions) and
`tests/test_structured_output_gate.py` (HTTP):

- Observe mode marks a prose reply `structured_output_failed`, returns it
  unchanged with 200, and attempts no repair.
- Enforce mode repairs once to the same URL with exactly one appended system
  message, and a second failure returns 422 without retrying again.
- A failed reply is not cached: a repeat request does not hit.
- A streamed JSON request reports `structured_output_unsupported`.
- The module's predicate agrees with `routing/catalog.py:_has_json_mode`.
- No model output reaches a header, the 422 body, or a validation result (G4).
- The full suite went from 868 to 928 with no existing test changed, which is
  what makes "observe by default is behavior-preserving" a measured claim
  rather than an intention.

Not yet run: a live harness tool call against a real slot. The hermetic suite
cannot show that a real model's JSON mode behaves as declared.

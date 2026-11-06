# PRD — Structured output contract

**Status:** Proposed — not shipped behavior
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

What happens next depends entirely on which backend routing picked, because the
three speak different dialects:

| Backend | Field it honors |
| --- | --- |
| llama.cpp | `grammar`, or `json_schema` on recent builds |
| Ollama | `format` |
| vLLM | `guided_json` |

A slot that does not recognize `response_format` ignores it and returns prose.
Nothing downstream notices: Imperium does not parse the reply, so the client
receives a 200 with a body it cannot load. For a tool-calling harness that is a
wrong answer dressed as a success, which ADR 0006 names the one failure the
product must not ship.

The gap is not that llama.cpp lacks grammars. It is that the translation and
the check belong in the middle, and the middle currently does neither.

## Scope

**Translate at the single forward boundary.** `_forward_payload` already is the
one place every chat request passes through on its way to a slot, and
`forward_to_upstream` is its upstream twin. Map `response_format` into the
selected backend's dialect there, from a declared capability on the target
rather than a guess — `upstreams/registry.py` already carries per-entry and
per-model capability declarations, and slots declare theirs in the fleet YAML.

**Check the answer.** When a schema was requested and the backend claims to
honor it, parse the reply against the schema before returning it. A reply that
does not conform is not returned as a success.

**One bounded repair.** On a non-conforming reply, re-ask once with the
validation error appended, then stop. Never loop: a second failure is reported,
not retried, so a bad model cannot turn one request into an unbounded spend.

**Say which happened.** Reason codes, in the established style:

| Code | Meaning |
| --- | --- |
| `structured_output_enforced` | translated to the backend's dialect and the reply validated |
| `structured_output_repaired` | first reply failed validation, the retry passed |
| `structured_output_unsupported` | the target declares no schema capability; the request was forwarded unchanged and the reply is not guaranteed |
| `structured_output_failed` | both attempts failed validation |

`structured_output_unsupported` is the important one. Today that case is
silent, and silence is what makes a prose reply look like a success.

## Non-goals

- A schema DSL of Imperium's own. `response_format` with JSON Schema is the
  contract; `grammar` passes through untouched for clients that send one.
- A dependency on `outlines` or any constrained-decoding library. The decoding
  happens in the backend; Imperium translates and verifies.
- Repairing more than once, or silently downgrading a schema request to prose.
- Streaming validation. A streamed response is forwarded as today, with
  `structured_output_unsupported` when a schema was asked for, until there is a
  reason to buffer.

## Seams it reuses

- `local_model_router/service/app.py:465` — `_forward_payload`, the single
  local forward boundary; `forward_to_upstream` for declared upstreams.
- `local_model_router/routing/catalog.py:170` — the existing JSON-mode
  capability detection, which already drives candidate ranking.
- `local_model_router/upstreams/registry.py` — `effective_capabilities()` and
  per-model overrides, the existing place a target declares what it can do.
- The reason-code convention in `service/routing_intent.py`.

## Open decisions

1. When the best-ranked slot cannot enforce a schema but a weaker one can, does
   routing prefer the weaker slot, or serve the stronger one with
   `structured_output_unsupported`? ADR 0006 argues for the former; latency
   argues for the latter.
2. Is a non-conforming second attempt a `422` to the client, or a 200 with the
   reason code and the raw text? Harness behavior should decide this.
3. Does the repair retry consume a fresh admission lease, or ride the original?

## Verification

- Hermetic tests per backend dialect: a `response_format` request produces the
  llama.cpp, Ollama and vLLM field respectively, from the declared capability.
- A non-conforming reply triggers exactly one retry, and a second failure
  reports rather than retries.
- A target with no schema capability yields `structured_output_unsupported` and
  an unchanged payload.
- No prompt body or schema content reaches telemetry (G4).
- Live: a real harness tool call against a local slot, before and after,
  showing the reply parses.

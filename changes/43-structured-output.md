# Added

- Structured-output checking (`local_model_router/structured_output.py`). When
  a request carries `response_format`, the reply's content is parsed before it
  is returned or cached. A model that ignores the field and answers with prose
  previously produced a 200 with a body the client could not load — ADR 0006's
  intolerable daily failure, arriving silently.
- `x-a0-router-structured-output` on chat responses, carrying
  `structured_output_valid`, `_repaired`, `_failed` or `_unsupported`, and the
  same code in telemetry.
- `A0_LMM_ROUTER_STRUCTURED_OUTPUT=1` enables enforcement: one repair attempt
  against the same target, then `422 structured_output_failed`. The repair
  rides the admission lease the request already holds, because a second
  acquire would deadlock at `max_active=1`.

# Changed

- A reply that fails validation is no longer written to the prompt cache.
  Caching it would serve the same bad body again as a hit and skip the check.
- `docs/prd/structured-output.md` is corrected. Its first draft said to
  translate `response_format` into each backend's native dialect from a
  per-slot declaration. Neither holds: `backend_type` carries the launch
  mechanism, not an API dialect, and Imperium only ever addresses
  OpenAI-compatible `/v1` surfaces, so there is nothing to translate to. The
  surviving half — that nobody checks the answer — is what shipped.

# Security

- Validation results, the repair instruction, response headers and the 422
  body carry the parser error and never the model's output (G4). The repair
  message names the error and the requirement only; the model already has its
  own output in context.
- The default posture is observe: the verdict is reported and the body is
  returned unchanged, so no existing client contract moves. The full suite went
  from 868 to 928 with no existing test altered.

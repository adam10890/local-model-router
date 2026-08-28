# Added

- Task disclosure (`local_model_router/disclosure/`): Imperium brokers tasks
  between agents and harnesses, and every handoff is now evaluated on two
  axes — how sensitive the content is, and how far the executor is trusted.
- An executor trust ladder (`local_uncensored`, `local_aligned`,
  `private_cloud`, `other_provider`) declared per slot, upstream, or agent via
  `trust_tier:`. Executors that declare nothing resolve to the least-trusted
  rung.
- A content-class map that caps the least-trusted executor allowed and fixes
  the form the content takes when sent (`skeleton_only`, `requirements_only`,
  `redacted_context`, `full`). `operator_data` and `security_surface` content
  never leaves the machine.
- `local-model-router disclosure` — `--list`, `--template`, `--classify`, and
  `--check` (with optional `--target`) for writing and validating task briefs
  before they reach a cloud model.
- Response headers `x-a0-router-trust-tier`, `x-a0-router-disclosure`, and
  `x-a0-router-disclosure-class` on declared-upstream forwards, and an
  `executor_tier:<tier>` reason code on completed routing decisions.
- ADR 0013 (executor trust ladder), ADR 0014 (cloud models get requirements,
  not purpose), and the operator policy in `docs/task-disclosure.md`.

# Changed

- `conf/upstreams.yaml` declares `trust_tier` on every entry. All shipped
  entries are `other_provider`; `private_cloud` is a claim about a contract
  and is never a default.

# Security

- The runtime gate is observe-only by default: a denied handoff is reported in
  headers and still forwarded, so routing behavior is unchanged. Set
  `A0_LMM_ROUTER_DISCLOSURE_ENFORCE=1` to return
  `403 disclosure_policy_violation` instead. The flag is read per request.
- Disclosure findings never quote matched text. Pattern ids, severities,
  counts, and line numbers are the only output, so scanner results are safe in
  headers, telemetry, CLI output, and HTTP responses. Message text is
  classified and scanned in memory and dropped; no prompt body is logged,
  stored, or returned.
- A malformed `conf/disclosure.yaml` override is rejected rather than silently
  replaced by more permissive defaults; the service logs a warning and falls
  back to the packaged rules.

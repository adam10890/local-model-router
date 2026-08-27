# DOX contract — local_model_router/disclosure

## Purpose

Imperium brokers tasks between agents and harnesses. Every handoff is a
disclosure decision on two axes: how sensitive the content is, and how far the
executor is trusted. This package owns both axes and the matrix between them.

## Ownership

- `disclosure.yaml` — packaged immutable default rules. Operators override it
  with `conf/disclosure.yaml`. Never contains secrets or real identifiers.
- `policy.py` — load and validate rules; `describe()` is the only public view.
- `trust.py` — resolve an executor (slot / upstream / agent) to a trust tier.
- `classifier.py` — deterministic content classification. No model call.
- `scanner.py` — forbidden-pattern detection.
- `decision.py` — the matrix evaluation and its reason codes.
- `brief.py` — brief templates, section validation, declared-class parsing.

## Local Contracts

- **A finding never carries matched text.** `scanner.Finding` reports pattern
  id, severity, count, and line numbers only. Scanner output is meant to be
  safe in headers, telemetry, CLI output, and HTTP responses alike; quoting a
  match would make this package the leak it exists to prevent.
- **Undeclared executors resolve to the least-trusted tier.** Missing or
  misspelled configuration must never widen who may receive content.
- **The most restrictive matching content class wins**, not the
  highest-scoring one. A brief mentioning both boilerplate and credentials is
  a security surface.
- Classification is a hint with stated evidence, never a silent verdict. Every
  decision carries reason codes in the routing-decision style.
- No network, no file writes, no prompt bodies stored anywhere.

## Work Guidance

- New content classes and trust tiers are founder taste (ADR 0007): amend
  `disclosure.yaml` and the policy doc together; do not invent classes in code.
- Keep the matrix data-driven — `decision.py` must stay free of hardcoded
  class or tier names.

## Verification

- `python -m pytest tests/test_disclosure_policy.py tests/test_disclosure_trust.py tests/test_disclosure_cli.py tests/test_disclosure_gate.py -q`

## Child DOX Index

No child AGENTS.md files yet.

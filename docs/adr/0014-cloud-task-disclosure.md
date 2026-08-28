# 0014 - Cloud models get requirements, not purpose

## Status

Accepted

## Context

Development work is increasingly delegated to cloud models. Left unwritten,
each task brief leaks a little more: why the feature exists, what it is worth,
what it is called, and what real data flows through it. Nothing in the repo
said what those models may be told, and `GOALS.md` G4 covered only the
router's own traffic, not the briefs we write by hand.

## Decision

A cloud model builds a **generic skeleton against product requirements**. We
integrate the data, the names, and the intent locally.

A brief states what must be true, never why the product wants it. Interface
shapes carry placeholder names. Examples are synthetic. File paths, hostnames,
ports, model ids, hardware, customer names, secrets, and prompt bodies never
appear.

Content is classified, and each class caps the least-trusted executor allowed
to receive it (ADR 0013) and fixes the form it takes when sent: `skeleton_only`,
`requirements_only`, `redacted_context`, or `full`.

## Consequences

- Two axes govern every handoff — content sensitivity and executor trust —
  evaluated together in `local_model_router/disclosure/`.
- `operator_data` and `security_surface` content never leaves the machine.
- The runtime gate ships **observe-only**: it reports the verdict in response
  headers and forwards unchanged. Enforcement is opt-in via
  `A0_LMM_ROUTER_DISCLOSURE_ENFORCE=1`, so turning it on is a separate,
  reversible decision that does not disturb the 1.0 beta evidence.
- Findings never quote what they matched. A leak check whose own output
  repeats the secret is not a leak check.
- Classification is a heuristic hint with stated evidence, never a silent
  verdict; a human can always override it.

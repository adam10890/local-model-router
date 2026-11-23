# 0003 - Simple Start first, advanced flags second

## Status

Accepted

## Context

Operators need a one-click path and experts need llama.cpp control. Both
must stay valid. The open question was development order, not exclusive
choice.

## Decision

We will ship and harden the simple Start path before full llama.cpp flag control.

Ship and harden path A before path B:

1. **A** — simple Start that works for the default operator.
2. **B** — full llama.cpp flag control for advanced operators.

Advanced controls must not break or replace the simple path.

## Consequences

- First-run, fleet start, and Hermes chat stay ahead of flag UIs.
- Advanced flag surfaces are additive, opt-in, and documented.
- Regression on the simple path blocks release more than missing a flag.

Negative: advanced operators wait for flag control, and a flag the simple path cannot express stays out until that path is safe.

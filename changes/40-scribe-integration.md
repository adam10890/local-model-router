## Added

- Independent `local:scribe` admission (one active, four waiting by default),
  a pinned local `a0_scribe` connection, and strict Scribe factuality evaluation.
- Evaluation suite versioning invalidates cached scores after case changes.

## Fixed

- Local-only intent survives pinned-route normalization and rejects explicit
  upstream forwarding with `403 local_only_violation` before admission.

## Security

- No prompt/response persistence, new dependencies, or automatic model starts.
  Client identity does not select the queue; the resolved slot role does.

## Operations

- Minor version 0.13.0. No database migration. Scribe remains optional and its
  example slot is disabled until a measured local model is configured.
- See docs/scribe-integration.md for migration, verification and rollback.

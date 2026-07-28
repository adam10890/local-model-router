## Added

- Per-upstream admission lanes and deterministic local-model evaluation.
- Safe evaluation snapshots through `GET /routing/evaluations`.

## Changed

- Local, explicit upstream, auto-upstream, and pinned harness chat requests
  now use one final telemetry lifecycle.
- Evaluator responses are bounded and optional reasoning traces are disabled
  so deterministic checks measure the requested answer.
- Model fingerprints use stable runtime and hardware identity fields, not
  changing free-memory readings, so unchanged evaluations are reused.
- A previously unreachable model is evaluated when its slot becomes healthy
  instead of being skipped only because its file fingerprint is unchanged.

## Fixed

- Dashboard harness/model mappings, Fleet search, chat errors, and sequential
  slot-health probing.

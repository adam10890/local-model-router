## Added

- Safe single-GPU Hermes model switching with recent-session selection,
  measured context metadata, detailed API/dashboard state, and a verified
  drain-first Windows shutdown path.

## Changed

- `slot_chat` now holds infrastructure settings while Hermes GPU profiles own
  model paths, templates, load flags, capabilities, and output defaults. The
  committed Hermes selection pin now defaults to Ornith.

## Fixed

- New requests receive `503 gpu_swap_in_progress` with `Retry-After` while
  active requests drain, including requests released from the local queue;
  unrelated explicit upstream requests remain available. Model starts use
  bounded backoff, retry without MTP,
  restore the last working configuration once, and stop retrying a failed
  target until an operator or target change resets the budget. A missing or
  disabled managed slot now disables the watcher without preventing router
  startup. Valid profile-file changes reset the exhausted-target budget, and
  state logs include elapsed time and a sanitized result code. Preparation
  failures, admission telemetry errors, and queued-client cancellation release
  their lease or queue slot exactly once; numeric and ISO Hermes session
  timestamps are supported, and stale busy/warning/runtime overlays are
  cleared only when their owning operation succeeds. Manual Stop reports
  `gpu_slot_stopped`, and bulk fleet operations retain per-slot results.
- Draining a slot closes ingress and waits until every accepted normal or
  streaming request finishes or disconnects. It never cancels an in-flight
  response: after the notice threshold it reports `waiting_for_requests` and
  keeps waiting. The wait itself is bounded by `global.drain_timeout` (15
  minutes by default) so that a request lease which is never released cannot
  wedge model swap, manual Stop and shutdown at once with no way back. Reaching
  that ceiling abandons the wait and reports `drain_timeout`; it still cancels
  nothing, so the running model keeps serving, the slot is not stopped, and no
  auto-restart attempt is consumed. The Windows shutdown CLI keeps its separate
  180-second client timeout and leaves the router running when the server-side
  drain is still active.

## Security

- Router and llama.cpp shutdown verifies PID, process creation time, and
  executable identity and never kills an unrelated process by port alone.
  The shutdown client bounds only its HTTP wait and leaves the verified router
  running while the server continues draining; an exit during a successful
  drain is handled as an idempotent success.

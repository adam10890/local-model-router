# 0015 - Hermes session chooses the single GPU GGUF

## Status

Accepted

## Context

The reference RTX 4090 has 24GB. Ornith-1.5-35B-A3B Q4 and
Qwen3.8-27B-OBLITERATED Q4 cannot share VRAM. Hermes records `/model` choices
on sessions in `state.db` and has a provider default in `config.yaml`.
Imperium previously had a static harness pin but no safe follower for that
selection.

A one-card replacement takes roughly a minute and creates a real availability
window. It is an operator-selected cold path outside the five-second
weak-to-strong promotion target in ADR 0009.

## Decision

The newest recognized Hermes selection from an open, non-archived session
active within 15 minutes is the desired GPU profile. At most 32 matching rows
are considered, ordered by `COALESCE(last_activity_at, started_at)`. If no
recent session qualifies, Imperium keeps a healthy loaded model. If no model
is loaded, it uses the configured default profile.

- Default name: `ornith`. Opt-in uncensored name: `uncensored` (profile
  `qwen38_27b`). `local` is an alias of `ornith`.
- One managed llama-server slot (`slot_chat`) owns host, port, role, and process
  lifecycle. `conf/hermes_gpu_profiles.yaml` owns the GGUF path, template,
  load flags, capabilities, configured context, and output default. This is a
  process replacement, not llama.cpp Router Mode.
- `BackendManager` owns one coordinator per slot. Watcher swaps, fleet actions,
  and auto-restart use it; a second operation returns
  `slot_operation_in_progress`, and auto-restart stays suspended throughout
  drain, stop, load, and restore. Manual Stop remains stopped until an explicit
  Start.
- Before stopping, Imperium validates the target model and template, closes
  request admission, and drains accepted non-streaming and streaming requests.
  After 60 seconds it reports `waiting_for_requests` and continues waiting;
  it does not cancel the active response. New requests receive OpenAI-compatible
  HTTP 503 `gpu_swap_in_progress` with `Retry-After`.
- Stop success, port release, process health, process ownership, and loaded
  model identity must be verified. Imperium never kills an unverified process
  merely because it owns the configured port.
- A target gets at most three starts, with 15- and 60-second backoff. MTP is an
  optimistic first attempt, not a compatibility claim. If that load fails, the
  next attempt uses the same GGUF without speculative flags and still counts
  toward the limit. Missing paths, occupied ports, and unverifiable ownership
  do not justify this fallback.
- After target failure, Imperium cleans up the process it started and tries
  once to reload the last working profile. The requested target still fails
  explicitly; it is never answered silently by the restored model. Exhausted
  retries or failed restore leave a stable operator-action state until the
  target/profile changes or an explicit Start resets the budget. Valid profile
  file changes are loaded between operations; unchanged polling does not reset
  the budget.
- After a verified load, the Hermes connection pin changes in process memory.
  An atomic state file stores only the last working profile id and successful
  load generation. It is recovery input, not evidence that a model is loaded,
  and normal swaps do not rewrite harness or fleet YAML.
- Status keeps `desired`, connection `pin`, and verified `loaded` GGUF
  separate. It also reports phase, duration, attempt, next retry, failure code,
  `configured_n_ctx`, `effective_n_ctx`, and measurement source.
- Effective context is probed from llama.cpp `/props`, with positive live
  `/v1/models` metadata as fallback. The `/props` shape is treated as a probe,
  not a guaranteed llama.cpp contract. An unavailable measurement is
  `Unknown`; configured 128K is not presented as measured.
- Client output limits are authoritative. When neither `max_tokens` nor
  `max_completion_tokens` is present, Ornith defaults to 16,384. The
  OBLITERATED profile keeps reasoning disabled, and `finish_reason=length`
  remains visible as truncation.
- Current profiles declare `supports_vision: false`; no mmproj is loaded.
  Vision requires a separate VRAM validation and profile change.
- WhatsApp continues to invoke Hermes `/model`; there is no separate
  WhatsApp-to-Imperium API or webhook. The `pnp` workflow remains deferred.

## Consequences

- A single card still has an unavoidable no-model window while the replacement
  or recovery profile loads, but accepted responses are not cut off and new
  requests fail with a retryable, specific code.
- A session selection cannot create a parallel load. A changed selection is
  handled serially after the current operation.
- SQLite read failures warn and leave a healthy model in place. Polling alone
  does not reset an exhausted retry budget.
- The Home and Chat views can show selection, connection pin, loaded GGUF,
  swap progress, failure code, and measured context without exposing session
  identifiers or conversation content.
- INFO state transitions include desired and loaded profiles, phase, elapsed
  operation time, attempt, and a sanitized result code.
- Carnice rows in `docs/1.0-beta-evidence.md` remain dated historical evidence;
  they are not current `slot_chat` operating instructions.

# Background Scribe integration

Imperium executes inference and stores safe request metadata. The Scribe client
owns notes and state through Pen & Paper. A failed inference skips the model
note; deterministic event recording continues. There is no cross-client journal.

## Configure and migrate

1. Install the router change (0.13.0) before the Scribe client change (0.2.0).
   Preserve existing local config and plugin code before changing either.
2. Configure a dedicated local slot with `role: scribe`, `model_id: scribe` and
   a deliberately verified `trust_tier`. The example slot is disabled. Do not
   assign an alignment label from a filename or assume available GPU memory.
3. Keep `a0_scribe.connections.default.model: scribe` in `conf/harnesses.yaml`.
   The pin is authoritative; an unavailable target returns
   `503 harness_model_unavailable` without cross-model fallback. Generic
   `model=scribe` routing is not this guarantee: ranked alternatives may include
   other roles even after removing chat from the configured failover chain.
4. Set the client's `imperium.base_url` to
   `http://host.docker.internal:9000/harnesses/a0_scribe/v1` in Docker, or
   `http://127.0.0.1:9000/harnesses/a0_scribe/v1` on the host. Configure
   `imperium.api_key_env: IMPERIUM_API_KEY`; put the router bearer key in that
   environment variable only. Verify container reachability separately. A host
   loopback listener is not proof a Docker client can reach it. Public binds
   require authentication. Never enable the no-auth override for this setup.
5. Run the client's `python execute.py`, then a synthetic inference. A successful
   GET `/models` proves connectivity, not model quality or installed-hook behavior.

The client sends low priority, its attribution headers and local-only routing.
`429 queue_full` means skip; no retry or alternate host. Three consecutive
failures open the client circuit for 120 seconds. Healthy or busy replies reset
that failure streak. Secrets stay in the environment and proxy/redirect behavior
must not carry content to a different target.

## Admission and privacy

`A0_FLEET_SCRIBE_MAX_ACTIVE=1` and `A0_FLEET_SCRIBE_MAX_QUEUE=4` are the defaults.
These bounds are independent of `A0_FLEET_MAX_ACTIVE` / `A0_FLEET_MAX_QUEUE`.
The actual selected slot's role chooses the queue; client headers cannot choose
it. `/fleet/status` exposes `queues["local:scribe"]`, and responses carry
`X-A0-Admission-Lane`. Streaming retains admission until completion/disconnect.
Low priority orders this queue; it does not preempt inference or reserve hardware.

A local-only request aimed at an explicit upstream or an upstream-pinned harness
returns `403 local_only_violation` without forwarding or acquiring that lane.
This includes `routing.local_only` and `privacy_mode: local_only` under the
existing intent precedence. Other explicit upstream requests retain their policy.
Telemetry and analytics must contain no prompt, response or bearer-key bodies.

## Evaluation and isolated CPU pilot

Run `python -m local_model_router evaluate-models --base-url <isolated-router> --force`
against a fleet containing only the reviewed test slot. Both Scribe cases must
pass: fact preservation and the structured failure/fix regression. Snapshots
contain scores, not answers; suite-version changes rerun cached evaluations.
Passing these bounded probes does not prove arbitrary summaries are factual.

Use a separate checkout, loopback ports, configuration/log directory,
`IMPERIUM_HOME`, and `TEMP`/`TMP` in the child process. Otherwise another port can
still share the live telemetry database or router ownership state. Disable the
Hermes watcher. Use synthetic events and an isolated Pen & Paper sink.

For a CPU pilot, start with 8192 context, four threads, one parallel sequence,
and a small existing model selected by the operator. With the installed engine,
`gpu_layers: 0` alone is insufficient because the backend can omit the flag.
Verify runtime support with `llama-server --help`, set `fit: false`, and use:

```text
--device none --gpu-layers 0 --fit off --no-kv-offload --no-op-offload
```

Inspect effective argv and verify no GPU allocation for that owned process.
Stop if resource/quality limits fail; do not silently try chat or a cloud model.
Use ten synthetic scenarios (including safe silence under busy/failure), require
no invented successes and completion within 60 seconds. Compare chat alone and
with CPU Scribe using three warmups and twenty samples per condition; treat a
p95 slowdown over 20 percent or new chat errors as a failed pilot. Missing live
chat evidence is Unknown, not Pass. Only stop processes created by the pilot.
An isolated worker/model test does not verify installed Agent Zero hooks.

## Verify, update and rollback

```powershell
python -m pytest tests/test_scribe_integration.py tests/test_model_evaluation.py tests/test_gpu_swap_api.py -q
python -m pytest tests/ -q
python -m pip list --outdated
```

Review dependency updates in a separate environment, install only the reviewed
versions, and repeat the same tests and synthetic inference. No new dependency is
required for this integration. Upgrade router before client. For packaged
installs use `imperium update --check`, then the reviewed update; for source
installs use a clean checkout of the reviewed revision and its existing install
procedure. Do not pull over unrelated local changes. Stop the Scribe worker
before rollback; restore the previous reviewed client/config and router package
(`imperium rollback` for a managed install). Preserve Pen & Paper state and local
fleet files. No schema migration, release tag or daily activation is automatic.

## Recorded pilot evidence

The [2026-10-30 isolated CPU pilot](evidence/scribe-cpu-pilot-2026-10-30.json)
proved connectivity, Scribe attribution/lane selection, content-free telemetry,
and cleanup of its owned processes. The candidate **failed** the quality gate:
one of the two Scribe evaluation cases passed, and one worker summary omitted
its failure count. Daily activation is blocked. Installed Agent Zero/Pen & Paper
hooks and the live-chat p95 comparison remain Unknown. Do not promote this
candidate from an inference/connectivity pass alone.

A separate read-only code review found no additional code blockers in router
`7c4122d` and client `cd658af`. It covered admission cleanup, privacy enforcement,
evaluation invalidation, HTTP/circuit behavior, worker config and redaction.
This review did not rerun the tests or verify installed hooks; direct concurrent
client calls are outside the current single-worker runtime evidence.

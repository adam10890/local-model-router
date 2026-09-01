# Spynel adoption plan

This plan records the bounded patterns Imperium may adopt from Spynel. Spynel
is not a dependency, and no Spynel source code is copied into Imperium.

## Verified baseline (2026-09-01)

- Review target: Spynel v0.9.1 at commit `109696e`.
- Repository base: Imperium `HEAD == origin/main` at `3542216` before opening
  `dev/harness-client-canary`; there is no four-commit sync gap.
- `docs/HARNESSES.md` records Hermes 0.21.0.
- Installed clients observed locally: Hermes 0.21.0, Agent Zero 2.11, Pi
  0.80.6, and Claude Code 2.1.220.
- Windows `.cmd` launchers require resolving the executable with
  `shutil.which` before `subprocess.run(..., shell=False)`; keep that step.
- Hermes rejects `-t none`; the canary uses its valid text-only `bot_room`
  toolset.

## Phase 1 — harness evidence (implemented)

- Keep endpoint smoke separate from installed-version and real-client
  evidence in schema v2.
- Run bounded local version probes and report official stable releases as
  optional metadata with `current` / `behind` / `ahead` alignment. Offline or
  rate-limited stable lookup remains `Unknown` and does not fail RC.
- Run Hermes with an isolated temporary home/workspace,
  `--ignore-user-config`, `--ignore-rules`, and no persisted prompt, response,
  key, executable path, or local path.
- Fail closed when the setup manifest has no replaceable `base_url`.
- Accept the fixed response token anywhere in stdout, then require the
  protected router analytics counter for `app_id=hermes` to increase. A client
  response without that evidence is `routing_unverified`, not Pass.
- Do not add a Pi canary until these guards remain green in full tests and a
  live Hermes run.

## Phase 2 — close G2 before feature expansion

- Preserve the sanitized local Hermes JSON as precursor evidence; a workflow
  run is still required for final RC status.
- Add Pi next using the same result contract, not a shared framework unless
  real duplication appears.
- Agent Zero and Claude Code remain `Unknown` until each has a safe isolated
  client invocation.
- Keep G2 `Partial` and do not start Chat V2 while other P0 beta gates remain
  open.

## Deferred, gated work

- Post-beta Chat V2 may add local `message_id` / `request_id` correlation and
  `queued`, `generating`, `completed`, `cancelled`, `interrupted`, and `error`
  states without automatic retry or server-side conversation storage.
- HIVE, not Imperium, owns durable task orchestration. Its minimal lease,
  claim journal, crash recovery, and duplicate-dispatch acceptance test are
  defined in [future-orchestration.md](future-orchestration.md).
- ACP remains an optional future executor adapter. Any material Spynel code
  reuse requires a fresh license review and preservation of its MIT notice.

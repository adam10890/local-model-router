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

## Re-review (2026-11-22)

- Review target: Spynel v0.16.5 (main at `1b19fb1`, 2026-11-18), seven minor
  versions after the v0.9.1 baseline above. Read from its documentation only;
  no Spynel binary was run and no Go source was read.
- Phase 1 stands. Spynel's evidence labels (Documented, Observed, Unverified)
  are already in use in `HARNESSES.md` as Documented, Observed, and `Unknown`
  for what no canary has shown.
- Where Spynel sits: above Imperium. It orchestrates external coding harnesses
  (Codex, Claude Code, Pi, Agent Zero CLI through `a0 acp`, other ACP agents).
  It serves no models, has no OpenAI-compatible surface, and has no notion of
  VRAM, queue depth, or provider budget; its `orchestrator.max_parallel` is a
  flat number. Imperium is the compute layer underneath its harnesses.
- Platform: Spynel's release documentation says Windows distribution is
  temporarily unsupported, so on this repository's primary platform it means
  WSL2. Router reachability from WSL2 is untested.
- Adopted as design evidence, no code copied:
  - A per-run isolation checklist for real-provider canaries, folded into Q4
    as the Agent Zero disposable-container canary.
  - The durable task contract (Markdown record, lease, claim journal, crash
    recovery test) stays the post-gate HIVE contract in
    [future-orchestration.md](future-orchestration.md); Spynel is a worked
    example of it, which bore on open questions 1 and 2; ADR-0017 settled
    both.
  - Offline, versioned, agent-readable documentation, queued in Q6 as MCP
    resources for harness setup.
- Not adopted: Spynel as a runtime dependency, its channels (Telegram,
  WhatsApp, TUI), and its hook protocol. The plugin-framework question stays
  open question 3.
- Risks to weigh before any integration: Spynel's default `sandbox` is
  `danger-full-access`; its review loops and 15-minute semantic heartbeat
  multiply model calls, which queue behind the local lane's single active
  request (`max_active: 1`); its own compatibility document says real-provider
  canaries have not run for every combination.
- Untested integration idea: a Spynel `harness.before` hook that asks
  `GET /compute/budget` before dispatching local work. Hook timeout and cancel
  behavior under load were not checked.
- Observed once, cause not found, not queued: `python -m local_model_router
  shutdown` answered `router_ownership_unverified` while `/health` was up and
  `state/router-process.json` was absent after a relaunch through `START.bat`.
  A later normal start wrote the file. Refusing to stop an unverified process
  is the intended G5 behavior; the missing file is what is unexplained.

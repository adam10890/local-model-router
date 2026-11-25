# Idea queue — the single FIFO backlog

This file is the **only** queue. Every other planning document in `docs/` holds
*detail*; this one holds *order*. If the two disagree about what comes next,
this file wins.

Before this file became the queue there were five places an idea could be
written down and no place that said which came first, so ideas accumulated and
branches were opened out of turn. One ordered list, and the discipline of
putting a new idea in it instead of in a new branch, is what that is meant to
fix.

## How the queue works

- **Entries are numbered `Q<n>` on arrival and never renumbered.** The number
  is an identity, not a priority.
- **Position is arrival order, and work goes to the lowest-numbered unblocked
  entry.** FIFO. An entry can be blocked by a gate (see `1.0-beta-evidence.md`);
  blocked entries are skipped, not reordered.
- **A new idea is checked against the queue before it is numbered.** If it
  advances, enables, or is a special case of an entry already queued, it is
  appended to that entry's **Folds in** list and inherits that entry's
  position. It gets no number of its own. Idea 5 supporting idea 2 becomes part
  of Q2, and Q2 does not move.
- **Only an idea that advances nothing already queued is numbered, at the
  tail.**
- **Nothing leaves the queue silently.** An entry leaves by shipping (moved to
  `## Shipped` with the version that carries it) or by explicit rejection
  (moved to `## Declined` with one line of why). Deleting an entry is not a
  way to make a decision.
- **An idea is not a branch.** While a task branch is open, a new idea is
  written here and nothing else happens to it. The rule, and the check that
  enforces it, are in [`development/git-workflow.md`](development/git-workflow.md).

## The queue

### Q1 — Observability surface

Export what the router already measures in a format something else can read.
`FleetStore` records status, queue depth, admission lane, duration, fallback
use, reason codes, cache status and token counts, and all of it is reachable
only through the bespoke `GET /routing/analytics`.

- **Detail:** [`prd/observability.md`](prd/observability.md)
- **Status:** queued — gated on the 1.0 beta gates
- **Slug:** `observability`
- **Folds in:**
  - Prometheus `/metrics` endpoint, from the original Router core list — this
    is the same idea and is now the body of the PRD.
  - `/routing/history` endpoint plus a dashboard view: the same store, a
    different projection. Build it as a view over the series this entry
    exposes rather than a second bespoke endpoint.
  - The deferred `/metrics` and `/routing/history` entries in the root
    `AGENTS.md` deferred list.

### Q2 — Cache-aware routing

Prefer a slot that already holds the prompt prefix, so the cache stops being
invisible to the routing decision.

- **Detail:** [`prd/cache-aware-routing.md`](prd/cache-aware-routing.md)
- **Status:** queued — gated on the 1.0 beta gates; wants Q1 to be measurable
- **Slug:** `cache-aware-routing`
- **Folds in:**
  - Complexity-based tiering and per-app token budgets (Archon-inspired): the
    same ranking seam, `rank_candidates`, and the same per-app accounting.

### Q3 — Herdr-inspired reliability slices

Sixty pinned source observations grouped into twenty-four bounded packages.
All Phase A existing-path reliability slices are shipped (`P01` event cursors
and resync in `0.16.2`; `P03` honest readiness + `P02` draft/focus continuity in
`0.16.3`; `P05` stop attribution + `P10` Windows installer recovery in
`0.16.4`). Remaining Phase B–E packages stay parked behind explicit client-hook,
post-beta Chat V2 (`Q4`), and `WB0` Workbench boundary gates.

- **Detail:** [`herdr-development.md`](herdr-development.md) (R01–R60),
  [`herdr-todo.md`](herdr-todo.md) (P01–P24 and their evidence),
  [`herdr-work-plan.md`](herdr-work-plan.md) (effort, value, dependencies)
- **Status:** queued — planning authorized, each slice selected explicitly
- **Slug:** `herdr`
- **Folds in:**
  - Worktree isolation per execution (Archon-inspired) is the same idea as
    R58 / P23; it lives with the Herdr packages, not separately.
  - Dynamic expert profiles (AutoGroq-inspired) overlap the Herdr
    role/readiness work; treat them as one body.

### Q4 — Close G2 with harness evidence

Six of seven beta goals are Partial. G2, client compatibility, is the one with
a defined path: real-client evidence per harness.

- **Detail:** [`spynel-adoption-plan.md`](spynel-adoption-plan.md) phase 2,
  measured against [`1.0-beta-evidence.md`](1.0-beta-evidence.md)
- **Status:** queued — this is the entry that unblocks Q1, Q2 and enforcement
  of the shipped structured-output gate
- **Slug:** `g2-harness-evidence`
- **Folds in:**
  - Chat V2 streaming states, the half of P02 deferred behind the Chat V2
    gate.
  - Turning on `A0_LMM_ROUTER_STRUCTURED_OUTPUT=1` by default: a shipped
    feature whose enforcement decision waits on these gates.
  - An Agent Zero canary in a disposable container with its own `usr` (never
    the operator's), wired with `scripts/connect_agent_zero.py` and run under
    a per-run authorization checklist and incident-stop conditions borrowed
    from Spynel's [provider-canary threat model](https://github.com/agent0ai/spynel/blob/main/docs/provider-canary-threat-model.md).
    This is what "a safe isolated client invocation" means for Agent Zero in
    [`spynel-adoption-plan.md`](spynel-adoption-plan.md). The web UI needs a
    login, so the canary drives the router route and Agent Zero's own preset
    resolver rather than a UI chat.
  - Q7 stage 1, the Hermes baseline with fakes: the effective busy mode, the
    harness-wide ingress gate test, and how long a launch turn is held by
    `503` / `Retry-After` during a simulated Studio lease. This is hermetic
    Hermes harness evidence, and the later WhatsApp canary is G2 evidence
    too. Adam recorded the fold-in on 2026-11-23. Q7 keeps its number for
    stages 2–5.

### Q5 — Replacement execution surface

The `/orchestrator/*` surface is deprecated. Anything that replaces it is
gated by the re-entry conditions already written down.

- **Detail:** [`future-orchestration.md`](future-orchestration.md); Spynel
  v0.16.5 re-review in [`spynel-adoption-plan.md`](spynel-adoption-plan.md)
- **Status:** blocked — the re-entry gates are not met. Task ownership is
  settled by [ADR-0017](adr/0017-task-ownership-commitments-for-intent-executors-for-execution-state.md):
  a replacement surface executes work, it does not own tasks or task policy
- **Slug:** `execution-surface`
- **Folds in:**
  - Declarative YAML workflows and a minimal runner (Archon-inspired): this is
    the replacement direction, not a separate idea.

### Q6 — Remaining accepted candidates

Smaller items accepted as directions with no PRD yet. They stay one entry
until one of them is picked up and earns its own.

- **Status:** queued
- **Slug:** `candidates`
- Per-app API keys and rate limits — extend `apps/profiles.py`
  (`conf/apps.yaml`) with per-`X-App-Id` credentials and budgets; today a
  single global bearer key is the only auth.
- Actionable cookbook — wire `GET /cookbook` output to the opt-in fleet
  control so a recommendation can be applied in one step.
- Cross-provider fallback breadth — per-role upstream preferences over the
  shipped upstream fallback lane.
- Whiteboard code extraction in the dashboard chat surface.
- GPU-profile or role pins after a model swap. Observed 2026-11-22: after the
  model behind GPU profile `ornith` was replaced, Agent Zero's static pin
  `ornith` answered `503 harness_model_unavailable`. Showing pinned-but-unservable
  harnesses in the alert center shipped in v0.16.0 (`harness_pin_drift` in
  `GET /ui/status`). Still open: whether the pin API may resolve a GPU profile
  or role alias (touches G3 "a pinned harness never silently changes its model"
  and is the founder's call; slot-id pin `slot_chat` already covers this
  today).

### Q7 — Hermes availability during Studio work

Keep the same Hermes chat responsive while Studio uses the shared GPU. Prove a
verified local standby, dependent task intake, strong-model planning and review
at GPU boundaries, and one bounded small-worker skill. Adam's requests live as
commitments in מעקב אדם, Hermes owns execution records and delivery, Studio
owns renderer attempts, and Imperium owns admission and GPU ownership.

- **Detail:** [`prd/hermes-chat-during-studio.md`](prd/hermes-chat-during-studio.md)
- **Status:** queued — task ownership is settled by ADR-0017 (commitments in
  מעקב אדם, execution in Hermes Kanban). Stage 1 runs under Q4. Stages 2–5
  are gated on beta stability and the Proposed ADRs for the standby route.
- **Slug:** `hermes-chat-during-studio`

### Q8 — llama.cpp v1.0.0 runtime pin (`b12359`)

Move the managed Windows runtime from nightly `b12110` to `b12359`, the binary
build named by the llama.cpp `v1.0.0` source release. Keep the current server
arguments, smoke on the live RTX machine, and roll back on any failed probe.
New flags (FP8 KV cache, MoE cache, sharding, audio) are follow-ups that each
need a measured profile change.

- **Detail:** [`superpowers/plans/2026-11-16-llama-cpp-v1.md`](superpowers/plans/2026-11-16-llama-cpp-v1.md)
- **Status:** queued — Task 5 needs the live machine and Adam's approval
- **Slug:** `llama-cpp-v1`
- **Update every version reference in the same change.** Before merging, run
  `git grep -n -E "b9987|b12110|min_llama_cpp"` and resolve every hit:
  - Change to `b12359`: `local_model_router/setup/runtime_catalog.json`
    (`recommended` tag, source, commit, and every asset name, URL, sha256 and
    size), `local_model_router/setup/model_catalog.json` `min_llama_cpp`, the
    `--tag` help example in `local_model_router/cli.py`, the `b12110`
    comments in `helpers/backends/docker_backend.py` and
    `subprocess_backend.py` (re-check the behavior they describe), and the
    tests that hard-code the recommended tag (`tests/test_setup_engine.py`,
    `tests/test_cli_commands.py`).
  - Decide explicitly: the Docker images
    (`ghcr.io/ggml-org/llama.cpp:server-cuda` / `:server` in
    `docker_backend.py` and `conf/llama_cpp_servers.example.yaml`) use
    floating tags. Pin them to the `b12359` build or document that Docker
    stays unpinned.
  - Leave unchanged, because they record history: `docs/evidence/*.json`,
    `docs/1.0-beta-evidence.md`, `changes/`, and the PR #24 notes in
    `scripts/branch_guard.py` and `tests/test_branch_guard.py`.
  - The live machine: `imperium update --tag b12359 --yes`, then confirm the
    manifest shows `runtime.tag` `b12359` and `previous_runtime.tag` `b12110`.

## Open questions

These are contradictions between accepted documents, not ideas. They are the
founder's to settle, and an entry that depends on one cannot start until it is
settled.

1. **Who owns task policy.** *Settled 2026-11-23 by
   [ADR-0017](adr/0017-task-ownership-commitments-for-intent-executors-for-execution-state.md).*
   Imperium does not own tasks or task policy. HIVE stays a proposal until two
   executors must coordinate one task. `future-orchestration.md` was aligned.
2. **The canonical task service has no named owner.** *Settled 2026-11-23 by
   ADR-0017.* מעקב אדם commitments hold what Adam asked to have done; the
   executor that runs the work owns execution state and links to the
   commitment ID. This is the canonical service for Herdr P16/WB2 and phase D.
3. **Plugins: framework or not.** The original backlog accepted a drop-in
   plugin folder; R09, P12 and the roadmap's 2.0 line all say no plugin
   framework. One of them has to go. Currently affects nothing in flight.

## Maintenance (not ideas)

Process and repository work that justifies a branch without being a queue
entry. Listed so `scripts/branch_guard.py --queue` can see the slug.

- `branch-convergence` — branch hygiene, the one-branch guard, and this queue.
- `spynel-rereview` — re-baseline the Spynel adoption plan against v0.16.5 and
  record the Spynel-derived queue entries above.
- `adr-steward` — adopt `tools/adr.py` for the decision log (ADR-0016) and
  the founder-directed wording pass on ADR-0001–0015.
- `task-ownership` — accept ADR-0016, record ADR-0017 (settles open questions
  1 and 2), and queue Q8.
- `core-reliability-and-security` — fix upstream structured-output/stream/embeddings
  lifecycle bugs, harden `FleetStore` and `stats_tracker`, and apply endpoint/Docker
  security hardening.
- `github-governance-and-beta-evidence` — fix ADR index formatting, add GitHub
  governance/CI quality gates, refresh beta evidence, and clean historical
  `a0_lmm_router` plugin references.
- `service-modularization` — extract pooled `SharedHttpClient`, split
  `service/app.py` into focused route modules (`context.py`, `routes_*.py`),
  and gate legacy `/orchestrator/*` behind `IMPERIUM_ENABLE_LEGACY_ORCHESTRATOR`
  with an explicit `Sunset` header (Proposed ADR-0020).
- `dashboard-modular-assets` — split `dashboard/index.html` into zero-build
  static CSS/JS assets at `/ui/assets/{name}`, tighten `/ui` CSP to
  `script-src 'self'`, and move stored browser API keys to `sessionStorage`.
- `streaming-usage-and-swap-wait` — record SSE streaming token usage into
  `FleetStore` and `usage_ledger`, fix pooled stream close guards, and add
  opt-in bounded request wait on `gpu_swap_in_progress` (Proposed ADR-0021).
- `pin-drift-openapi-mcp-harnesses` — add optional `harness_pin_drift`
  readiness alert in `GET /ui/status`, expose MCP `harnesses://list` and
  `harnesses://{harness_id}/setup` resources, serve OpenAPI `3.1.0` at
  protected `GET /openapi.json`, and add HTTP contract tests.

## Shipped

Newest first. An entry here is traceable to the version that carries it.

- **Pin-drift alert, MCP harness resources, and OpenAPI 3.1.0 schema** (0.16.0) —
  optional `harness_pin_drift` alert in `GET /ui/status`, read-only MCP
  `harnesses://list` and `harnesses://{harness_id}/setup` resources, and
  protected `GET /openapi.json` schema generation (`service/openapi_spec.py`).
- **SSE streaming usage accounting and opt-in bounded Hermes swap wait** (0.15.0,
  Proposed ADR-0021) — `SseUsageExtractor` (`service/sse_usage.py`) for local
  and upstream SSE streams plus bounded `swap_wait_seconds` /
  `IMPERIUM_SWAP_WAIT_SECONDS`.
- **Service route modularization and static dashboard assets** (0.14.3–0.14.4,
  Proposed ADR-0020) — incremental split of `service/app.py` into
  `context.py`, `http_client.py`, and `routes_*.py`; opt-in legacy
  `/orchestrator/*` gate with `Sunset` header; static CSS/JS at
  `/ui/assets/{name}`, `script-src 'self'` CSP, and `sessionStorage` key
  storage.

- **Structured-output checking** (0.14.0) — a reply to a `response_format`
  request is parsed before it is returned or cached; observe by default,
  `A0_LMM_ROUTER_STRUCTURED_OUTPUT=1` repairs once then returns 422.
  [`prd/structured-output.md`](prd/structured-output.md).
- **Task disclosure and the executor trust ladder** (0.12.0, ADR 0013/0014) —
  `local_uncensored` → `local_aligned` → `private_cloud` → `other_provider`,
  with a content-class map capping what each may receive and in which form.
  [`task-disclosure.md`](task-disclosure.md).
- **Health-probe TTL cache** (0.7.0, PR #8) —
  `helpers/smart_router/health.py` (`global.health_cache_ttl` /
  `A0_LMM_ROUTER_HEALTH_CACHE_TTL`).
- **Ranked-candidate failover chains** (0.7.0, PR #8) —
  `helpers/llama_cpp_manager.py`, `service/routing_intent.py`
  (`failover_chain:ranked|config` reason codes).
- **Opt-in upstream-aware auto-routing** (0.7.0, PR #8) —
  `A0_LMM_ROUTER_AUTO_UPSTREAMS=1` plus a declared `models:` list per upstream;
  local-first preserved, embeddings stay local.

## Declined

Nothing yet. An idea belongs here the moment it is rejected, with the reason,
so it is not proposed again from memory.

## Selection rules

- Local-first contracts win: loopback default, bearer auth, no prompt bodies in
  telemetry, explainable decisions with reason codes.
- Prefer items that reuse existing seams (`FleetStore`, `rank_candidates`,
  fleet control) over new subsystems.
- Anything touching orchestration must pass the re-entry gates in
  [`future-orchestration.md`](future-orchestration.md).
- An entry must name the document that holds its detail. An entry with no
  detail anywhere is an idea, not a plan, and stays in Q6 until it has one.

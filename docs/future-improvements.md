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
The recommended first slice is P01, the counter regression, then P03 honest
readiness.

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

### Q5 — Replacement execution surface

The `/orchestrator/*` surface is deprecated. Anything that replaces it is
gated by the re-entry conditions already written down.

- **Detail:** [`future-orchestration.md`](future-orchestration.md)
- **Status:** blocked — the re-entry gates are not met, and the ownership
  question in `## Open questions` must be answered first
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
- Incremental split of `service/app.py` into api / routing / telemetry seams,
  tests covering each seam first, never big-bang.
- Actionable cookbook — wire `GET /cookbook` output to the opt-in fleet
  control so a recommendation can be applied in one step.
- Cross-provider fallback breadth — per-role upstream preferences over the
  shipped upstream fallback lane.
- Whiteboard code extraction in the dashboard chat surface.

## Open questions

These are contradictions between accepted documents, not ideas. They are the
founder's to settle, and an entry that depends on one cannot start until it is
settled.

1. **Who owns task policy.** `future-orchestration.md` says Imperium continues
   to own task policy; `spynel-adoption-plan.md` says HIVE, not Imperium, owns
   durable task orchestration. Both cannot be the contract. Blocks Q5.
2. **The canonical task service has no named owner.** `herdr-development.md`,
   the work plan's phase D and `herdr-todo.md` P16/WB2 all require one, and no
   document says which system provides it. Blocks the later Herdr packages in
   Q3.
3. **Plugins: framework or not.** The original backlog accepted a drop-in
   plugin folder; R09, P12 and the roadmap's 2.0 line all say no plugin
   framework. One of them has to go. Currently affects nothing in flight.

## Maintenance (not ideas)

Process and repository work that justifies a branch without being a queue
entry. Listed so `scripts/branch_guard.py --queue` can see the slug.

- `branch-convergence` — branch hygiene, the one-branch guard, and this queue.

## Shipped

Newest first. An entry here is traceable to the version that carries it.

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

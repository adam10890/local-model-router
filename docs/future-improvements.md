# Future improvements — deferred ideas backlog

This backlog records improvement proposals that were reviewed and accepted as
*candidate* directions, not committed work. Selection and prioritization are
deferred until the in-progress component specifications land; each item must be
re-validated against the DOX contracts before implementation. Items are ordered
within each group by expected value.

Proposals 1-3 of the original review were implemented in PR #8 and are listed
under "Shipped" for traceability.

## Shipped (PR #8, 0.7.0)

- Health-probe TTL cache — `helpers/smart_router/health.py`
  (`global.health_cache_ttl` / `A0_LMM_ROUTER_HEALTH_CACHE_TTL`).
- Ranked-candidate failover chains — `helpers/llama_cpp_manager.py`,
  `service/routing_intent.py` (`failover_chain:ranked|config` reason codes).
- Opt-in upstream-aware auto-routing — `A0_LMM_ROUTER_AUTO_UPSTREAMS=1` plus a
  declared `models:` list per upstream in `conf/upstreams.yaml`; local-first
  preserved, embeddings stay local.

## Router core

- **Prometheus `/metrics` endpoint** — export request/queue/failover counters
  already collected by `service/fleet_manager.py:FleetStore`; keep prompt
  bodies and secrets out, same as `/routing/analytics`.
- **Per-app API keys + rate limits** — extend `apps/profiles.py`
  (`conf/apps.yaml`) with per-`X-App-Id` credentials and request budgets;
  today a single global bearer key is the only auth.
- **`/routing/history` endpoint + dashboard view** — expose recent routing
  decisions from the SQLite `requests` table with reason codes; no prompt
  content.
- **Incremental split of `service/app.py`** — extract api/routing/telemetry
  layers per the root AGENTS.md renaming rule (tests must cover each seam
  first; never big-bang).
- **Actionable cookbook** — "apply recommendation" action wiring `GET
  /cookbook` output to the opt-in fleet control
  (`A0_LMM_ROUTER_ENABLE_FLEET_CONTROL=1`); one click starts a recommended
  slot.

## Inspired by AutoGroq (github.com/jgravelle/AutoGroq)

- **Dynamic expert profiles** — derive richer role/persona signals from the
  prompt than the current alias/task-type inference in
  `routing/catalog.py:role_from_chat_body`; add "team presets" that bundle
  several models for one pipeline.
- **Drop-in plugin folder** — auto-discovered scoring strategies and upstream
  adapters (analogous to AutoGroq's skills folder) so new providers do not
  require registry edits; capabilities must stay declared, never guessed.
- **Whiteboard code extraction** — the dashboard chat surface extracts fenced
  code blocks from replies into a separate copyable pane.

## Inspired by Archon (github.com/coleam00/Archon)

- **Declarative YAML workflows / minimal runner** — multi-stage pipelines
  (plan on a quality model, generate on coder, validate on fast). This is the
  natural replacement direction for the deprecated `/orchestrator/*` surface;
  it is additionally gated by everything in `future-orchestration.md`.
- **Complexity-based tiering + token budgets** — per-app cumulative token
  tracking with throttling; route simple steps to small models and reserve
  large models for reasoning-heavy steps.
- **Cross-provider fallback breadth** — extend the shipped upstream fallback
  lane with per-role upstream preferences and richer failover classification.
- **Worktree isolation for runner executions** — once a runner exists, each
  execution gets an isolated git worktree to allow safe parallel runs.

## Selection rules

- Local-first contracts win: loopback default, bearer auth, no prompt bodies
  in telemetry, explainable decisions with reason codes.
- Prefer items that reuse existing seams (`FleetStore`, `rank_candidates`,
  fleet control) over new subsystems.
- Anything touching orchestration must pass the re-entry gates in
  `future-orchestration.md`.

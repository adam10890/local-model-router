# DOX contract - docs

## Purpose

Durable documentation for operators, contributors, and coding agents.

## Ownership

- `../GOALS.md` owns product outcomes and 1.0 beta exit gates; docs must not
  contradict that contract.
- `1.0-beta-evidence.md` owns Pass/Partial/Fail/Unknown records for beta
  gates; missing evidence is Unknown, not Pass.
- `1.0-beta-roadmap.html` owns accepted sequencing toward beta; it must stay
  aligned with GOALS priorities and non-goals.
- `PROVIDER.md` owns standalone provider runbook details, including the
  local llama.cpp vs upstream (`ollama_cloud`) operator rule.
- `INTEGRATIONS.md` owns copy-paste client setup snippets for MCP and
  OpenAI-compatible clients.
- `HARNESSES.md` owns harness terminology, dedicated connection contracts,
  and the new-harness setup flow.
- `COMPUTE-BUDGET.md` owns the compute-budget/token-economy layer: declared
  upstream `limits`, the live Codex usage reader, `GET /compute/budget`,
  budget-aware routing, the local usage ledger, and the dashboard's Compute
  Providers tab.
- `1.0-beta-roadmap.html` owns the accepted living product roadmap toward
  the standalone Imperium beta; keep decisions, gates, and deferred ideas
  current.
- `future-orchestration.md` owns the legacy-orchestration deprecation and the
  gates for any replacement execution surface.
- `future-improvements.md` owns the deferred improvement-ideas backlog
  (candidate directions only; labeled deferred until selected).
- `adr/` owns accepted product/engineering decisions from operator judgment
  (Hermes priority, compute roles, and similar). Vision stays in README;
  outcomes stay in `GOALS.md`.
- `hot-cold-policy.md` owns the accepted-direction design for warm-weak /
  promote-strong local compute (not shipped behavior until implemented).
- `needle-bootstrap-evaluation.md` records why Needle remains an Advanced,
  experimental candidate rather than the Windows bootstrap default.
- `development/` owns Git workflow, branch catalog rules, and collaboration
  process.

## Local Contracts

- Documentation must describe current behavior and stable operating rules, not
  speculative plans. The beta roadmap is the explicit exception: it tracks
  accepted product direction and must label deferred ideas as deferred.
- Do not include secrets, API keys, prompt bodies, or machine-local fleet
  config values.
- Keep `AGENTS.md`, `CONTRIBUTING.md`, `CLAUDE.md`, and development workflow
  docs aligned when process rules change.

## Work Guidance

- Prefer concise operational checklists over narrative history.
- Link to the owning code path or config file when a doc makes a technical
  claim.
- If a doc change updates workflow or responsibilities, update the nearest
  owning DOX file in the same change.

## Verification

- For docs-only changes: `git diff --check`.
- If a doc updates commands that should be executable, run the command when it
  is safe and hermetic; otherwise state why it was not run.

## Child DOX Index

- `development/AGENTS.md` - Git workflow, branch catalog, and agent handoff
  rules.

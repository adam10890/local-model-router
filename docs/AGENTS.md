# DOX contract - docs

## Purpose

Durable documentation for operators, contributors, and coding agents.

## Ownership

- `PROVIDER.md` owns standalone provider runbook details.
- `INTEGRATIONS.md` owns copy-paste client setup snippets for MCP and
  OpenAI-compatible clients.
- `HARNESSES.md` owns harness terminology, dedicated connection contracts,
  and the new-harness setup flow.
- `1.0-beta-roadmap.html` owns the accepted living product roadmap toward
  the standalone Imperium beta; keep decisions, gates, and deferred ideas
  current.
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

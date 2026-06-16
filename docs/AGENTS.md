# DOX contract - docs

## Purpose

Durable documentation for operators, contributors, and coding agents.

## Ownership

- `PROVIDER.md` owns standalone provider runbook details.
- `development/` owns Git workflow, branch catalog rules, and collaboration
  process.

## Local Contracts

- Documentation must describe current behavior and stable operating rules, not
  speculative plans.
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

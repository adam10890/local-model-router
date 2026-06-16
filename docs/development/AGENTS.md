# DOX contract - docs/development

## Purpose

Development-process documentation: Git branch lifecycle, branch catalog rules,
merge hygiene, and human/agent handoff conventions.

## Ownership

- `git-workflow.md` owns branch naming, branch states, merge rules, cleanup,
  and current branch catalog expectations.

## Local Contracts

- `main` is the only permanent integration branch.
- Active work is classified as `dev/<slug>`, verified work as `ready/<slug>`,
  and throwaway exploration as `spike/<slug>`.
- Agent scratch prefixes such as `codex/<slug>` and `claude/<slug>` are
  temporary and must be classified before final merge or long-lived handoff.
- Branch cleanup is part of the workflow: merged branches should be deleted
  locally and remotely after `main` contains their commits.

## Work Guidance

- Keep workflow rules explicit enough that Codex, Claude Code, and a human can
  follow the same branch lifecycle.
- Avoid duplicating the full policy in many files; link back to
  `git-workflow.md` from higher-level docs.
- Update the branch catalog section when long-lived active branches are
  created, reclassified, merged, or retired.

## Verification

- `git status --short --branch`
- `git branch --all --verbose --no-abbrev`
- `git diff --check`

## Child DOX Index

No child AGENTS.md files yet.

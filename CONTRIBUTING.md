# Contributing

This repository uses a DOX-style `AGENTS.md` tree plus a small Git branch
catalog. The goal is simple: keep `main` usable, make work-in-progress easy to
identify, and make agent handoffs explicit.

## Required reading

- `AGENTS.md` at the repository root.
- Any child `AGENTS.md` on the path you will edit.
- `docs/development/git-workflow.md` before creating, merging, deleting, or
  handing off branches.
- `CLAUDE.md` when using Claude Code.

## Branch contract

- `main` is the shippable trunk. It should contain reviewed or intentionally
  accepted work only.
- `dev/<slug>` is active implementation work from `main`.
- `ready/<slug>` is a verified merge candidate. No new scope goes there; only
  fixes, test updates, documentation, or conflict resolution.
- `spike/<slug>` is throwaway research. If it graduates, port the useful work
  into a clean `dev/<slug>` branch.
- `codex/<slug>` and `claude/<slug>` are allowed for short-lived agent scratch
  work. Before merge or handoff, classify the work into `dev/` or `ready/`, or
  document why it is being merged directly.

Use lowercase, hyphen-separated slugs, for example `dev/routing-history`.

## Standard workflow

```powershell
git fetch --prune origin
git switch main
git pull --ff-only origin main
git switch -c dev/<slug>
```

Work in small commits. Before handoff or merge, run the relevant verification
from `AGENTS.md`. When the branch is ready for final review:

```powershell
git switch -c ready/<slug>
```

Merge verified candidates into `main` with a merge commit when preserving the
phase boundary is useful:

```powershell
git switch main
git pull --ff-only origin main
git merge --no-ff ready/<slug> -m "merge: <slug>"
python -m pytest tests/ -q
git push origin main
```

After the merge, delete merged branches locally and remotely:

```powershell
git branch --merged main
git branch -d ready/<slug>
git push origin --delete ready/<slug>
```

## Safety rules

- Do not use `git reset --hard`, force-push, or delete an unmerged branch
  unless the user explicitly asks for that exact operation.
- Do not overwrite another agent's or human's uncommitted changes.
- Keep `.env` and machine-local fleet config out of commits.
- Update the nearest `AGENTS.md` when a change alters durable behavior,
  workflow, ownership, verification, or file responsibilities.
- Put branch status and unresolved risks in handoffs instead of leaving them
  implicit in commit messages.

# Git workflow and branch catalog

This project keeps Git simple and explicit. Branch names are the first layer of
cataloging; this document is the second layer for rules, current inventory,
and handoff expectations.

## Branch states

| Prefix | State | Meaning | Merge target | Cleanup |
| --- | --- | --- | --- | --- |
| `main` | trunk | Shippable project state | none | never delete |
| `dev/<slug>` | active development | Work is still changing | `ready/<slug>` or `main` for tiny docs-only changes | delete after merge |
| `ready/<slug>` | verified candidate | Scope is closed; only fixes, docs, tests, or conflicts | `main` | delete after merge |
| `spike/<slug>` | experiment | Research or throwaway proof | none; port useful pieces into `dev/` | delete when learned from |
| `codex/<slug>` | agent scratch | Codex-created short-lived work | classify into `dev/` or `ready/` | delete after handoff |
| `claude/<slug>` | agent scratch | Claude Code-created short-lived work | classify into `dev/` or `ready/` | delete after handoff |

Legacy `feature/<slug>` branches should not be used for new work. If one
exists and is merged, delete it. If it is still active, reclassify it to
`dev/<slug>` or `ready/<slug>` before final merge.

## Current branch inventory

As of 2026-06-16:

| Branch | Status | Action |
| --- | --- | --- |
| `main` | active trunk | Branch new work from here. |
| `ready/orca-inspired-routing` | verified candidate | Local-first+ catalog/routing phase; ready for merge review after full verification and DOX updates. |
| `feature/dashboard-v2` | merged into `main` | Local and remote branch deleted. |
| `feature/fleet-control` | merged into `main` | Local and remote branch deleted. |

When new long-lived branches are opened, add a row with the owner, state, and
next action. Remove or mark the row retired when the branch is merged and
deleted.

## Start work

```powershell
git fetch --prune origin
git switch main
git pull --ff-only origin main
git switch -c dev/<slug>
```

Use a short lowercase slug, for example `dev/routing-history` or
`dev/app-api-keys`.

If Codex or Claude Code creates an automatic scratch branch such as
`codex/<slug>` or `claude/<slug>`, keep it short-lived. Before another worker
picks it up, either merge it into a `dev/<slug>` branch, rename it, or record
the classification in the branch inventory.

## Promote to ready

A branch can move to `ready/<slug>` only after:

- the intended scope is implemented
- the nearest `AGENTS.md` contracts have been checked and updated if needed
- relevant tests or checks have been run
- known risks are documented in the handoff or PR

For a clean branch rename:

```powershell
git branch -m dev/<slug> ready/<slug>
git push origin :dev/<slug> ready/<slug>
git push --set-upstream origin ready/<slug>
```

For preserving the active branch while creating a candidate:

```powershell
git switch dev/<slug>
git switch -c ready/<slug>
git push --set-upstream origin ready/<slug>
```

## Merge to main

Before merging:

```powershell
git fetch --prune origin
git switch main
git pull --ff-only origin main
git log --oneline main..ready/<slug>
```

Merge with a merge commit for feature phases or workflow changes:

```powershell
git merge --no-ff ready/<slug> -m "merge: <slug>"
python -m pytest tests/ -q
git push origin main
```

For tiny docs-only changes, a direct commit on `main` is acceptable when the
working tree is clean and the user asked for immediate repository maintenance.
Run `git diff --check` before committing.

## Cleanup merged branches

Only delete branches that Git confirms are merged into `main`:

```powershell
git branch --merged main
git branch -r --merged main
git branch -d ready/<slug>
git push origin --delete ready/<slug>
git fetch --prune origin
```

If a branch is not merged, do not force-delete it unless the user explicitly
asks for that exact destructive action.

## Agent handoff checklist

Every Codex, Claude Code, or human handoff should include:

- current branch and branch state
- files changed
- commits created or intentionally left uncommitted
- verification commands and results
- unresolved risks, skipped checks, or manual smoke tests

Agents must not silently overwrite each other's work. If the working tree is
dirty and the changes are not yours, stop and ask before switching branches,
rebasing, stashing, or deleting anything.

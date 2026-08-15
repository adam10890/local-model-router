# Git workflow and branch catalog

This project keeps Git simple and explicit: one task uses one temporary branch,
`main` is the only permanent branch, and cleanup happens in the same maintenance
window as the merge. Branch names are the first layer of cataloging; this
document is the second layer for rules, current inventory, and handoff
expectations.

## Default lifecycle

`main` → `dev/<slug>` → rename to `ready/<slug>` → merge to `main` → delete.

- Keep one active `dev/` or `ready/` branch by default. Parallel branches need
  an explicit owner and inventory entry.
- Promote by renaming the existing branch; do not fork a second `ready/` branch.
- Never merge an old branch merely because Git does not recognize a prior
  squash merge. Prove whether its content is already in `main` first.
- Finish verification, merge, push, remote-SHA confirmation, and cleanup before
  starting the next task.

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

As of 2026-08-15:

| Branch | Status | Action |
| --- | --- | --- |
| `main` | active trunk | Branch new work from here. Beta gate Phases 0–5 merged (`3b3b006`…`7708717`). Evidence: `docs/1.0-beta-evidence.md`. |

Add an inventory row only when a parallel or long-lived branch is explicitly
approved. Remove the row when the branch is merged and deleted; do not retain
historical merged rows here because Git is the history.

## Start work

```powershell
git fetch --prune origin
git switch main
git pull --ff-only origin main
git status --short --branch
git switch -c dev/<slug>
```

Use a short lowercase slug, for example `dev/routing-history` or
`dev/app-api-keys`. Do not start when `main` contains unrelated uncommitted
work; preserve or hand off that work explicitly first.

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
```

Push the temporary branch only when another worker or CI needs it. If a remote
`dev/<slug>` already exists, rename it remotely rather than leaving both refs.

## Merge to main

Before merging:

```powershell
git fetch --prune origin
git switch main
git pull --ff-only origin main
git log --oneline main..ready/<slug>
git merge-tree (git merge-base main ready/<slug>) main ready/<slug>
```

Any conflict markers from `merge-tree` block the merge. Resolve them on the
`ready/` branch, rerun verification, and repeat the preflight.

Merge with a merge commit for feature phases or workflow changes:

```powershell
git merge --no-ff ready/<slug> -m "merge: <slug>"
python -m pytest tests/ -q
git push origin main
git ls-remote --heads origin main
```

The local `git rev-parse main`, `origin/main`, and the SHA returned by
`ls-remote` must match before cleanup.

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
git branch --all --verbose --no-abbrev
```

If a branch is not merged, do not force-delete it unless the user explicitly
asks for that exact destructive action.

## Previously squash-merged branches

The repository default is a merge commit, not a squash merge. If GitHub or a
maintainer already squash-merged a branch, do not merge that branch again.
Compare it with the known commit already in `main`:

```powershell
git diff --quiet ready/<slug> <merged-sha>
git rev-parse ready/<slug>^{tree}
git rev-parse <merged-sha>^{tree}
```

Only when the diff is empty, both tree hashes match, no PR remains open, and
the user authorizes deletion may the stale local branch be removed with `-D`.
Record both SHAs in the handoff.

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

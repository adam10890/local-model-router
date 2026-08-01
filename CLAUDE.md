# Claude Code instructions - local-model-router

Claude Code must treat the DOX tree as the source of truth for repository
context.

## Read before editing

1. Read `AGENTS.md` at the repository root.
2. Identify every file or folder you expect to touch.
3. Walk from the root to each target path and read every `AGENTS.md` on that
   route.
4. If touching Git history, branches, release flow, or handoff process, read
   `CONTRIBUTING.md` and `docs/development/git-workflow.md`.

Do not rely on memory from another session. Re-read the relevant contract in
the current session.

## Operating rules

- The router is the product. Do not reintroduce Agent Zero imports or runtime
  assumptions.
- Keep edits small and behavior-preserving unless the user explicitly asks for
  a larger migration.
- Never commit secrets, prompt bodies, `.env`, or
  `conf/llama_cpp_servers.yaml`.
- Respect local-first defaults: loopback bind, bearer auth when configured,
  and opt-in fleet lifecycle control only.
- Use the branch lifecycle in `docs/development/git-workflow.md`: `main` for
  shippable trunk, `dev/<slug>` for active work, `ready/<slug>` for verified
  merge candidates, and `spike/<slug>` for throwaway experiments. Claude's own
  `claude/<slug>` branches are allowed as short-lived scratch branches, but
  classify or merge them into `dev/` or `ready/` before handoff.
- Keep one task branch by default. Promote `dev/` to `ready/` by renaming the
  same ref, then verify, merge, confirm GitHub's `main` SHA, and delete the
  temporary branch before starting another task. Do not re-merge a stale
  squash-merged branch.

## Verification

- For code changes, run `python -m pytest tests/ -q` unless the user narrows
  scope and you clearly report the narrower check.
- Run `python -m py_compile` on touched Python files.
- For docs-only changes, run `git diff --check`.
- Report the exact commands run and any skipped checks.

## Handoff

Every handoff must include:

- current branch and whether it is `dev/`, `ready/`, or `main`
- files changed
- verification commands and results
- follow-up risks or manual checks

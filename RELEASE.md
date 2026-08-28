# Release Governance

Apply this checklist to every code change before merge.

## Required Checks

1. Classify the SemVer impact and bump `local_model_router/__init__.py` on
   every merge, so `main` always describes itself. Additive API features are
   minor; compatible fixes are patch; breaking API changes are major.
   Docs-only and test-only changes do not bump. The version moves on merge;
   tagging is separate (see Cutting a release).
2. Record user-visible behavior in `CHANGELOG.md`.
3. Add one Markdown fragment under `changes/` named `<n>-<slug>.md` with
   `Added`, `Changed`, `Fixed`, or `Security` headings as applicable. `<n>` is
   one past the highest number already in `changes/`, not a PR number — the
   two diverged long ago, and reusing a PR number collides with an existing
   fragment. Check with:
   `ls changes/ | sed -E 's/^([0-9]+)-.*/\1/' | sort -n | tail -1`.
4. Update operator, API, setup, and contributor documentation affected by the
   change. Do not document speculative behavior.
5. Run `python -m py_compile` on every touched Python file.
6. Run `python -m pytest tests/ -q` without a fleet, GPU, Docker, or network.
7. Run `git diff --check` and inspect staged scope for secrets, local config,
   prompt bodies, and unrelated files.
8. For provider changes, run the safe `/health` smoke. Run the live
   `/v1/chat/completions` smoke only when a fleet is available and report when
   it was not available.
9. For release-affecting merges, confirm `docs/1.0-beta-evidence.md` G4 /
   gate 6 still records zero known security invariant violations (prompt
   bodies and secrets absent from telemetry and config previews; public
   no-auth binds refused unless explicitly acknowledged).

Do not merge while any applicable check fails. Keep release fragments for the
repository audit trail; a release commit may consolidate them into the
changelog but must not silently drop their content.

## Cutting a release

Merging bumps the version; it does not cut a release. A release is an operator
step, taken deliberately:

1. Confirm `local_model_router/__init__.py` carries the version being released.
2. Rename `## [Unreleased]` in `CHANGELOG.md` to `## [X.Y.Z] - <date>`, matching
   that version exactly, and open a fresh `## [Unreleased]` above it.
3. Tag the release commit `vX.Y.Z` and push the tag.

**Agents do not create or push tags.** Without this step a version exists only
as a heading in `CHANGELOG.md` and nothing in git records which commit it was —
which is how `0.6.0` came to be documented as a release the package never
reported.

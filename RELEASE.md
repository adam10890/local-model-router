# Release Governance

Apply this checklist to every code change before merge.

## Required Checks

1. Classify the SemVer impact and update `local_model_router/__init__.py` when
   the change is being released. Additive API features are minor; compatible
   fixes are patch; breaking API changes are major.
2. Record user-visible behavior in `CHANGELOG.md`.
3. Add one Markdown fragment under `changes/` named `<pr>-<slug>.md` with
   `Added`, `Changed`, `Fixed`, or `Security` headings as applicable.
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

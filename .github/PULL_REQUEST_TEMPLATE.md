## Summary

- <!-- Describe what changed and why -->

## Verification

- [ ] `python -m compileall -q local_model_router tests scripts/smoke_harnesses.py`
- [ ] `python -m pytest tests/ -q` (with coverage gates)
- [ ] `python tools/adr.py lint --base origin/main`
- [ ] `git diff --check`

## Release Governance (`RELEASE.md`)

- [ ] `local_model_router/__init__.py` bumped (if code changed)
- [ ] `CHANGELOG.md` updated under `## [Unreleased]`
- [ ] `changes/<n>-<slug>.md` fragment added
- [ ] G4 privacy check: no secrets, local paths, or prompt bodies in telemetry/previews/tests

## ADR Handoff

- Created: none
- Superseded: none
- Pending: none
- Constraints applied:

# GitHub governance, CI quality gates, beta evidence, and historical A0 cleanup

### Added

- Added `SECURITY.md`, `.github/dependabot.yml`, and
  `.github/PULL_REQUEST_TEMPLATE.md`.
- Added `ruff` (`E9`, `F63`, `F7`, `F82`) to `pyproject.toml` and added an
  unconditional `quality` CI job in `.github/workflows/tests.yml` running Ruff,
  `tools/adr.py lint`, static dashboard contract tests, and advisory
  `pip-audit`.
- Standardized GitHub Actions workflows onto `actions/checkout@v7` and
  `actions/setup-python@v6`.

### Changed

- Removed obsolete historical `a0_lmm_router` plugin references, links,
  docstrings, and temp defaults while keeping active Agent Zero harness setup
  (`https://github.com/agent0ai/agent-zero`) and backward-compatible env vars
  intact.
- Defaulted `usage_ledger.py` state to Imperium's state directory with
  `IMPERIUM_USAGE_LEDGER_PATH` and `A0_USAGE_LEDGER_PATH` support.
- Refreshed `docs/1.0-beta-evidence.md` with verified passing CI run IDs,
  synced `README.md` version, and removed the merged `dev/g2-harness-evidence`
  row from `docs/development/git-workflow.md`.

### Fixed

- Filtered `>` blockquote lines out of the `Status` table cell in
  `tools/adr.py index` and regenerated `docs/adr/README.md`.
- Replaced bare Python 3.11+ `BaseExceptionGroup` references in tests with a
  Python 3.10-safe `builtins` lookup so Ruff passes cleanly under `py310`.

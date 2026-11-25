# 54 — Herdr P05 & P10: Stop cause attribution and Windows installer recovery

- Version: `0.16.4`
- Branch: `dev/herdr`
- Queue entry: `Q3` (`docs/herdr-todo.md#p05`, `docs/herdr-todo.md#p10`, R34, R46, R47, R48, R51)
- Proposed ADR: none (hardening existing setup/shutdown stop attribution and Windows installer recovery paths)

## Summary

1. Implemented first-stop-wins cause/actor/request attribution in `SetupEngine` (`local_model_router/setup/engine.py`), `POST /setup/cancel` (`routes_setup.py`), `POST /fleet/prepare-shutdown` (`routes_fleet.py`), and `imperium shutdown` (`cli.py`), separating `cancel_requested`, `transport_lost`, `stopped_confirmed`, and `outcome_unknown` while scrubbing secret- or prompt-shaped inputs.
2. Hardened `installer/windows/Install-Imperium.ps1` and `Rollback-Imperium.ps1` with strict child-path validation, safe staging/retirement of `Imperium.previous` so a missing `Imperium` directory never loses its backup on retry, automatic restore on promotion failure, and surviving-backup path reporting when rollback fails.
3. Added promotion-failure recovery verification to `scripts/test_windows_cleanroom.ps1` and real PowerShell entrypoint fault-injection + raw `HKCU\Environment\Path` preservation tests in `tests/test_windows_packaging.py`, plus stop-attribution tests in `tests/test_setup_engine.py`, `tests/test_setup_api.py`, and `tests/test_cli_commands.py`.

## Verification

- `.\.venv\Scripts\ruff.exe check local_model_router tests tests_e2e scripts tools`
- `.\.venv\Scripts\python.exe tools/adr.py lint --base origin/main`
- `.\.venv\Scripts\coverage.exe run --source=local_model_router -m pytest tests/ -q` (full suite, zero `--ignore`) + 4 coverage gates
- `./scripts/build_windows_bundle.ps1` + `./scripts/test_windows_cleanroom.ps1`

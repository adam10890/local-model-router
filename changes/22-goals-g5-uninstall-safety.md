# Fixed

- Hardened Windows uninstall to the same per-user Programs path guard as
  rollback, stop the managed runtime via `STOP.bat` before deleting the
  install tree, and ship `Uninstall-Imperium.bat` in the Windows bundle.
- Kept models and settings under `%LOCALAPPDATA%\Imperium` after uninstall,
  and added hermetic packaging assertions for the uninstall contract.

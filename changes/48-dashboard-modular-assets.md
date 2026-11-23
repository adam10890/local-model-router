# 48 — Dashboard modular static assets and tighter CSP

- Version: `0.14.4`
- Branch: `dev/dashboard-modular-assets`

## Summary

1. Extracted inline `<style>`, i18n dictionary, and dashboard JavaScript from `local_model_router/dashboard/index.html` into zero-build static assets under `local_model_router/dashboard/assets/` (`styles.css`, `i18n.js`, `app.js`).
2. Replaced inline JS token/version injection with `<meta name="imperium-setup-token">` and `<meta name="imperium-version">` tags and external `<link>` / `<script defer>` references.
3. Added strict filename validation `dashboard_asset()` and `dashboard_bundle_text()` in `local_model_router/dashboard/__init__.py`, registered `GET /ui/assets/{name}` in `routes_harnesses.py` (`Cache-Control: no-cache`, `X-Content-Type-Options: nosniff`), and included `dashboard/assets/*.css` and `dashboard/assets/*.js` in `pyproject.toml` package data.
4. Tightened `/ui` CSP to `script-src 'self'` (keeping `style-src 'self' 'unsafe-inline'` for dynamic progress bar and utility widths) and moved `imperium.apiKey` storage to `sessionStorage` with automatic migration from any legacy `localStorage` entry.

## Verification

- `node --check local_model_router/dashboard/assets/i18n.js` & `app.js`
- `.\.venv\Scripts\ruff.exe check local_model_router tests scripts tools`
- `.\.venv\Scripts\python.exe tools/adr.py lint --base origin/main`
- `.\.venv\Scripts\coverage.exe run --source=local_model_router -m pytest tests/ --ignore=tests/test_branch_guard.py -q` + 4 coverage gates
- GitHub Actions `Chromium dashboard gate` & `PR Windows cleanroom`

# Security Policy

## Supported Versions

Imperium (`local-model-router`) is currently pre-1.0 (`0.14.x`). Security fixes are applied to `main` and released on the active `0.x` line.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities, credential leaks, or local privilege issues.

Instead, report vulnerabilities privately through [GitHub Security Advisories](https://github.com/adam10890/local-model-router/security/advisories/new) on `adam10890/local-model-router`. Include steps to reproduce, the affected endpoint or CLI command, and any relevant sanitized output.

## Security Invariants

Imperium enforces the following contracts by default (see `GOALS.md` G4 and `AGENTS.md`):

- **Loopback bind by default:** The service binds to `127.0.0.1` and refuses to bind publicly unless `A0_LMM_ROUTER_API_KEY` is configured or `A0_LMM_ROUTER_ALLOW_PUBLIC_NO_AUTH=1` is explicitly set.
- **No prompt bodies or secrets in telemetry:** Persisted SQLite telemetry, usage ledgers, `/diagnostics/report`, `/config/preview`, and task-disclosure findings redact secrets and never store or quote prompt text.
- **Opt-in mutations and upstream routing:** Fleet lifecycle mutation (`A0_LMM_ROUTER_ENABLE_FLEET_CONTROL=1`), config writes (`A0_LMM_ROUTER_ENABLE_CONFIG_WRITES=1` + bearer key), disclosure blocking (`A0_LMM_ROUTER_DISCLOSURE_ENFORCE=1`), and upstream auto-fallback (`A0_LMM_ROUTER_AUTO_UPSTREAMS=1`) are disabled unless explicitly opted in.
- **Loopback setup protection:** `/setup/*` endpoints accept only loopback requests carrying the ephemeral per-process `x-setup-token`.

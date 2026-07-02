# Compute Monitor

## Added

- `/fleet/status` now includes a `compute` block with local GPU, CPU, and RAM
  telemetry and a short-lived cache for dashboard polling.

## Fixed

- The service imports and health endpoint work after the PR #4 integration.
- Hardware probes run outside the ASGI event loop and degrade to explicit
  unavailable values instead of failing fleet status.

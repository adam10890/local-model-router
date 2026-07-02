# Compute Monitor Recovery Design

## Goal

Restore a bootable service and expose accurate, best-effort GPU, CPU, and RAM
telemetry from `GET /fleet/status` without blocking the ASGI event loop or
making fleet availability depend on local monitoring tools.

## Design

`local_model_router.helpers.compute_monitor` owns hardware collection only.
It uses `nvidia-smi` for NVIDIA GPU data and the required `psutil` dependency
for cross-platform CPU and RAM data. It does not construct an observer or
duplicate slot state; `service.app` already owns the configured observer.

The monitor returns a small immutable snapshot with two serializations:

- `to_dict()` for the additive `compute` response block.
- `vram_summary()` for the existing `vram` contract (`total_gb`, `used_gb`,
  `available_gb`, `source`).

`/fleet/status` runs the synchronous hardware probe through
`asyncio.to_thread`. A per-app monotonic TTL cache prevents dashboard polling
from spawning `nvidia-smi` for every request. Probe failures are logged and
fall back to `vram_unknown_summary()` plus an unavailable `compute` block;
monitoring must never take down routing or health endpoints.

## Compatibility

The existing `vram` keys remain stable. `compute` is additive and contains a
timestamp, GPU entries, CPU utilization, and RAM totals in MiB. `/health`,
slots, queue, agents, model residency, and fleet-control fields are unchanged.

## Verification

Hermetic tests replace subprocess and psutil calls. They cover successful GPU
parsing, unavailable or malformed GPU output, CPU/RAM conversion, snapshot
serialization, fleet-status integration, failure fallback, and cache reuse.
The full test suite runs without a GPU, live fleet, Docker, or network.

## Release Governance

This additive feature increments the minor version to `0.3.0`, adds an
Unreleased changelog entry and release fragment, documents the API contract,
and establishes `RELEASE.md` as the repository release checklist.

# Changelog

All notable changes to this project are documented here.

## [Unreleased]

## [0.3.0] - 2026-07-03

### Added

- Best-effort NVIDIA GPU, CPU, and RAM telemetry in `GET /fleet/status`.
- Five-second hardware snapshot caching without blocking the ASGI event loop.

### Fixed

- Restored service startup and `/health` after the broken compute-monitor
  integration merged in PR #4.
- Preserved the existing `vram` response and safe fallback when hardware
  probes are unavailable.

# Changelog

All notable changes to this project are documented here.

## [Unreleased]

## [0.5.0] - 2026-07-05

### Added

- OpenAI-compatible `POST /v1/embeddings`, routed through the same local slot
  selection used by chat and exposed through MCP.

### Changed

- MCP now acts only as an authenticated HTTP client of the router instead of
  constructing a second backend manager and routing path.
- Harness setup and connection guides now live as expandable rows inside the
  Harnesses dashboard tab.
- Standalone config lookup now uses an explicit safe override or repository
  config, without inherited Agent Zero container paths.
- Removed unused legacy manager, token-budget/VRAM estimates, JSON request
  accounting, no-op cloud policy, and non-serving AirLLM registration.

### Fixed

- `START.bat` waits for Docker Desktop when available and degrades cleanly when
  it is not, while configuring Ornith as the primary Docker Model Runner model.

## [0.4.0] - 2026-07-03

### Added

- Dedicated OpenAI-compatible URLs and setup manifests for Hermes, Pi, Agent
  Zero, and optional Claude Code local-mode harness connections.
- Canonical `conf/harnesses.yaml` profiles, legacy app-profile fallback, and
  opt-in authenticated atomic config writes with backups.
- Harness-first dashboard cards with pinned models, copyable setup, endpoint
  verification, last-seen state, and a guided add-harness form.

### Changed

- Clarified that harnesses own their internal roles; the router supplies one
  compute path per harness, with Agent Zero's chat/utility split as the only
  current exception.

### Fixed

- Pi and Hermes no longer need to bypass the router through raw model-server
  ports or share the generic auto-routing URL.

## [0.3.0] - 2026-07-03

### Added

- Best-effort NVIDIA GPU, CPU, and RAM telemetry in `GET /fleet/status`.
- Five-second hardware snapshot caching without blocking the ASGI event loop.

### Fixed

- Restored service startup and `/health` after the broken compute-monitor
  integration merged in PR #4.
- Preserved the existing `vram` response and safe fallback when hardware
  probes are unavailable.

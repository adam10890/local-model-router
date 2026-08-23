# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added

- Beta 1.0 gate program (repo-side): Windows uninstall path guards, living
  evidence log (`docs/1.0-beta-evidence.md`), G4 config-preview redaction
  attestation, first-run/recovery operator checklists, harness version matrix
  + expanded smoke scripts, and hermetic dashboard EN/HE smoke.
- Machine-local opt-in for declared upstream providers through
  `A0_LMM_ROUTER_ENABLED_UPSTREAMS`, without editing committed defaults.
- Declared per-upstream usage `limits` (rolling `"5h"`/`"7d"` windows,
  `max_tokens`/`max_requests`) and a `subscription` provider `type` for
  CLI-driven providers with no HTTP surface (Codex, Ollama Cloud's CLI path),
  in `conf/upstreams.yaml`.
- A live Codex/ChatGPT usage reader (`~/.codex/auth.json`, read-only,
  percent-of-window only) and an append-only local usage ledger
  (`A0_USAGE_LEDGER_PATH`, 35-day retention) as the accounting fallback for
  other declared-limit providers.
- `GET /compute/budget` — local hardware headroom plus every provider's
  budget status (`ok`/`warn`/`exhausted`/`unknown`/`tracked`).
- Budget-aware `POST /routing/request`: exhausted upstream candidates are
  dropped, near-limit ones are flagged, and the response carries a `budget`
  status map plus new `est_input_tokens`/`est_output_tokens`/`quality`
  request fields.
- MCP `compute_budget()` and `route_task()` tools — both recommend-only, like
  the rest of the routing MCP surface.
- A "Compute Providers" tab in the Advanced dashboard showing live/estimated
  usage windows per provider plus this computer's own hardware headroom.
- The dashboard now has a persistent alert button with a count and a bilingual
  drawer that separates configuration faults from system faults and links to
  the existing remediation screens.
- The protected Work Pages pilot now supports atomic ticket leases, owned
  progress/completion, a Hermes planning skill, and a Pi worker extension.

### Fixed

- `imperium doctor` now verifies the callable dependency capabilities used by
  the router, reports sanitized recovery guidance for broken namespace
  packages, and preserves its existing machine-readable dependency codes.
- The Windows bundle workflow now tests Python 3.12 and validates tag builds
  against package metadata instead of a stale hard-coded release version.
- Advanced dashboard navigation is always available, simple model choices stay
  filtered, pinned tool-path labels replace the older custom-harness wording,
  and leftover local debug telemetry has been removed.
- Managed subprocess fleet status now probes live process health, automatically
  retries crashed slots within the configured limit, and closes the alert
  drawer before navigating to recovery controls.
- Remote fleets now fail lifecycle requests immediately instead of waiting for
  a startup probe, and the dashboard shows external-server recovery guidance
  instead of non-functional start/stop controls.
- Agent Zero is now present in the canonical harness configuration, and its
  generated setup uses the supported Agent Zero 2.7 `other` provider with
  separate Docker-reachable Main and Utility Chat Completions URLs.
- Optional MCP installs now stay on the compatible 1.x SDK after 2.0 removed
  the FastMCP import surface used by Imperium.
- Docker Model Runner now declares tool support, so its Ornith Q8 model is no
  longer reported as tool-incapable.
- Managed llama.cpp processes now report stable startup/health failure codes,
  exit codes, restarts, and uptime, and clear stale errors after recovery.

## [0.9.0] - 2026-07-17

### Added

- Per-target admission lanes with optional upstream `max_active` and
  `max_queue` limits, plus lane telemetry in fleet and routing APIs.
- Deterministic local-model evaluation through `imperium evaluate-models`,
  reusable ranking hints, and `GET /routing/evaluations`.
- The Models page can persist and rescan a shared GGUF directory; installed
  files from that directory are available in first-run setup and the local
  chat model selector.

### Changed

- Chat requests now share one final lifecycle across local, explicit,
  auto-selected, and harness-pinned targets; streaming holds admission until
  completion, failure, or client disconnect.
- The dashboard now renders real harness connections and lane capacity,
  filters Fleet rows, summarizes model/routing details, and loads independent
  status surfaces concurrently.
- Managed llama.cpp setup uses its configured models directory for the Qwen3
  first-run download and enables directory-backed model hot-swapping.

### Fixed

- Upstream requests now record their actual final result instead of bypassing
  telemetry or stopping at `forwarded_upstream`.
- Slot health probes run concurrently and reuse the configured TTL cache.
- Chat errors render their message instead of `[object Object]`, installed
  model details resolve from Cookbook data, and empty chat sends are disabled.
- Setup tests no longer depend on a machine-local `llama-server` executable
  when verifying offline runtime planning.
- Setup no longer crashes when Python is installed near the filesystem root.
- `STOP.bat` now replaces stale router processes reliably under Windows
  Terminal by validating the process that owns the configured port.
- Long installed-model names, metadata, and paths now wrap inside their
  dashboard cards instead of overflowing the card boundary.

## [0.8.0] - 2026-07-14

### Added

- A resumable Windows-first wizard that discovers hardware, existing model
  servers, llama.cpp installations, GGUF files, occupied ports, and prior
  Imperium configuration before proposing changes.
- Managed, versioned llama.cpp and Qwen3 1.7B Q8 installation with explicit
  consent, checksums, progress, smoke tests, repair, and rollback.
- A conservative 4K first-run context and live available-memory preflight that
  asks users to close other model servers or applications before retrying.
- A self-contained per-user Windows bundle and isolated clean-room validation
  path for systems without Python, Git, or Docker.
- An explicit application rollback launcher, release checksum verification,
  and third-party license notices for bundled/offline assets.
- A unified `GET /ui/status` readiness response and task-oriented Simple
  dashboard with a separate Advanced workspace.

### Changed

- Rebuilt first run as a six-stage flow and made native llama.cpp the default;
  Docker and existing servers remain optional.
- Added consistent icons, light/dark themes, English/Hebrew localization, RTL,
  responsive states, and browser-local chat persistence.
- Kept the authenticated `/orchestrator/*` API for compatibility, marked its
  responses deprecated, and removed it from dashboard navigation.
- Bundled the Agent Library dependency and an immutable fallback catalog so
  packaged installations expose the same four agents as source checkouts.

### Fixed

- Windows-reported adapter memory is no longer described as dedicated VRAM,
  and inferred Vulkan support retains its real evidence and confidence.
- Existing repository-local configuration is preserved during upgrade instead
  of incorrectly opening first-run onboarding.
- Setup storage and runtime failures return actionable remediation instead of
  unhandled errors or silent backend changes.
- Persisted runtime PIDs are verified against their executable, creation time,
  and model command before Imperium will stop them.

## [0.7.0] - 2026-07-11

### Added

- Built-in `GET /agents` catalog and `POST /agents/{id}/runs` endpoint backed
  by Pydantic AI through the router's own OpenAI-compatible Chat Completions
  surface.
- Per-agent routing intent, including local-only sovereignty, bounded input,
  and timeout handling. Agent traffic remains identifiable in routing
  analytics, including auto-upstream fallback records.

### Changed

- Windows setup/start/stop scripts now install the agent runner, derive its
  loopback self-call URL, and honor a configured router port when stopping.

## [0.6.0] - 2026-07-11

### Added

- Ranked local candidate chains for deterministic failover when the preferred
  slot is unhealthy or cannot serve the request.
- A configurable health-probe TTL cache shared by routing and failover.
- Opt-in upstream-aware auto-routing that preserves local-first behavior and
  requires each upstream to declare its available models.

### Changed

- Routing decisions record ordered candidates, failover reason codes, and
  upstream forwarding outcomes without storing prompt bodies.
- Embeddings remain local even when automatic upstream fallback is enabled.

## [0.5.0] - 2026-07-05

### Added

- OpenAI-compatible `POST /v1/embeddings`, routed through the same local slot
  selection used by chat and exposed through MCP.
- A stdlib-only harness smoke command that verifies `/models` and a short
  completion for every configured dedicated connection.
- A minimal GitHub Actions gate for compilation and the hermetic test suite.

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
- `GET /v1/models` now probes local slots and serving upstreams concurrently,
  so unavailable targets consume one timeout window instead of accumulating.
- The development extra now installs `httpx` rather than the unrelated
  `httpx2` package, and provider smoke payloads omit removed routing fields.

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

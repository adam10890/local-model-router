# Added

- A resumable Windows-first setup wizard, hardware discovery, managed
  llama.cpp/model installation, and self-contained clean-room packaging.
- A readiness API and task-oriented Simple dashboard with a separate Advanced
  workspace, consistent icons, themes, English/Hebrew localization, and RTL.
- A packaged fallback catalog for the four built-in agents so source and
  self-contained installations expose the same Agent Library.

# Changed

- Made native llama.cpp the recommended first-run path while keeping Docker
  and existing servers as explicit optional choices.
- Replaced sample broken-looking configuration with an onboarding state and
  one recommended next action.
- Hid the legacy orchestration surface from the dashboard while preserving its
  authenticated API with an explicit deprecation response header.

# Fixed

- Distinguished Windows-reported graphics memory from verified dedicated VRAM
  and labelled inferred Vulkan support with its actual confidence.
- Preserved existing repository-local configuration during upgrade and
  returned actionable setup-storage errors instead of unhandled HTTP 500s.
- Kept Qwen3 1.7B Q8 as the default with a 4K managed context, while treating
  low currently available RAM as a transient close-apps-and-rescan condition.
- Included Agent Library dependencies and its default catalog in Windows
  release bundles.
- Added safe application rollback, bundle checksum validation, offline-only
  enforcement, and process ownership checks before stopping managed runtimes.

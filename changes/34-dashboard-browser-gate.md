# Dashboard browser gate

- Added a Python Playwright browser extra, a hermetic real-Chromium suite, and
  a path-filtered pull-request plus nightly workflow.
- Separated static dashboard contracts from browser evidence and retained
  screenshots/traces only for failing cases.
- Guarded Hermes model pinning behind API authentication and explicit config
  writes, with keyboard and responsive accessibility coverage.

### Changed

- Hermes requests that arrive during a Studio lease can now be held and forwarded the moment the lease ends, instead of a 503 with a fixed 120s Retry-After. Opt in with `lease_wait_seconds` in `conf/hermes_gpu_profiles.yaml` (or `IMPERIUM_LEASE_WAIT_SECONDS`), max 840s to stay under Hermes's 900s local TTFB watchdog. Default 0 keeps the old behavior; a lease longer than the budget still falls back to the 503.

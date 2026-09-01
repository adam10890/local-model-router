## Added

- Harness smoke schema v2 with bounded installed/stable version probes and an
  isolated real Hermes client canary. RC evidence fails closed when endpoint,
  installed-client, routing, or canary evidence is `Unknown`; stable release
  lookup is compared and reported without becoming a network gate.

## Fixed

- Generate Hermes context configuration from its pinned local slot and keep
  the committed Hermes pin aligned with the Qwen3.8 27B local fleet model.

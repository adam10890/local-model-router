### Added

- The nightly CPU cleanroom job also installs Hermes v0.23.5 (git tag, editable install so its git-derived version is observable) and runs the isolated Hermes client canary next to Pi (`--rc --harness pi --harness hermes`). Hermes refuses windows below 64K, so with `-HarnessCanary` the cleanroom serves the offline Qwen3 1.7B model at a real 65,536-token window (YaRN factor 2 over its native 32K, q8 KV cache) before the router starts (Q4, G2).

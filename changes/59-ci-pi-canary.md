### Added

- The nightly CPU cleanroom job (`windows-validation.yml`) now installs Pi 1.5.2 and runs the isolated Pi client canary against the offline `qwen3-1.7b-q8` model (`test_windows_cleanroom.ps1 -HarnessCanary`). Its sanitized `harness-smoke.json` is uploaded with the CPU evidence, giving G2 a workflow artifact instead of only a local run (Q4).

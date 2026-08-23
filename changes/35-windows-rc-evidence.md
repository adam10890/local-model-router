## Added

- Separate Windows PR, nightly CPU lifecycle, and opt-in packaged NVIDIA gates.
- Sanitized machine-readable provider, harness, and cleanroom evidence.
- Exact sanitized runtime transitions for packaged lifecycle evidence.

## Fixed

- Setup repair can recover a verified managed configuration after confirmation.
- Cleanroom rollback and uninstall preserve fixed data paths and avoid desktop changes.
- Bundle assembly no longer publishes a release automatically from a pushed tag.
- The development extra includes the build backend used by isolated bundle assembly.
- Runtime update discovery skips incompatible semantic releases and accepts
  only rolling builds with the complete expected SHA-256-digested asset set.

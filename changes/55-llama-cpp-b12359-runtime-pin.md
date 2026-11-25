# 55 — llama.cpp v1.0.0 (`b12359`) managed Windows runtime pin

## Summary

- Updated `SetupEngine._tagged_runtime_release` so requesting source-only tag `v1.0.0` fails before any network call with `invalid_release` naming rolling build `b12359`, and updated `imperium update --tag` help and `docs/PROVIDER.md` operator guidance (including explicit note that optional Docker images remain on upstream floating tags unless operator-pinned).
- Added unit tests locking `b12359` CUDA 12.4 asset verification (excluding `cuda-13.5`) and `SubprocessBackend._build_command` flag invariants (no `--defrag-thold`, `--allow-unauthenticated-tools`, `--media-allow-private-urls`, `f8_e4m3` KV, MoE cache, sharding, or `--sleep-idle-seconds`).
- Verified `imperium update --tag b12359 --yes` on the live Windows RTX 4090 setup (`runtime.tag = b12359`, `previous_runtime.tag = b12110`, `GET /health` `ok`, `POST /v1/chat/completions` HTTP 200 directly and via `:9000`, `GET /props` `effective_n_ctx = 131072`, verified `b12359-cuda12/llama-server.exe` process and `--no-reasoning-preserve` argv), then retargeted `runtime_catalog.json` and `model_catalog.json` (`min_llama_cpp`) to `b12359`.

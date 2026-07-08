# Added

- Router-backed OpenAI-compatible embeddings for MCP and API clients.
- A stdlib-only harness smoke command and a minimal GitHub Actions test gate.

# Changed

- Removed duplicate manager/routing paths and unused standalone-extraction code.
- Consolidated agent connection guides into expandable Harnesses rows.
- Updated startup defaults to Ornith and made Docker Desktop startup tolerant.

# Fixed

- Prevented MCP, config discovery, and model lifecycle code from relying on
  inherited Agent Zero container paths.
- Probed model-serving slots and upstreams concurrently in `GET /v1/models`.
- Corrected the development dependency from `httpx2` to `httpx` and removed
  stale routing fields from provider smoke payloads.

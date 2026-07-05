# Added

- Router-backed OpenAI-compatible embeddings for MCP and API clients.

# Changed

- Removed duplicate manager/routing paths and unused standalone-extraction code.
- Consolidated agent connection guides into expandable Harnesses rows.
- Updated startup defaults to Ornith and made Docker Desktop startup tolerant.

# Fixed

- Prevented MCP, config discovery, and model lifecycle code from relying on
  inherited Agent Zero container paths.

# Needle as an Imperium bootstrap model

Status: **experimental candidate - not a Windows first-run default**

Needle may become a useful always-on `tool_router` lane. Its small size and
single-shot function-selection focus are attractive, but it is not a GGUF
model and does not run through the managed llama.cpp runtime used by Imperium
0.8.0.

It is excluded from the approved first-run path because adding it would also
add a second managed runtime whose Windows installation, updates, checksums,
rollback, offline behavior, and OpenAI-compatible contract are not yet
verified.

## Proposed role

Needle may be offered under Advanced as an opt-in tool-router sidecar after:

1. An authoritative, versioned Windows runtime is available.
2. Install, update, repair, rollback, and offline-start pass on clean Windows.
3. The runtime passes Imperium's tool-call API contract.
4. A held-out English/Hebrew evaluation covers tool selection, argument JSON,
   no-tool decisions, and safe fallback.
5. Failure falls back to the active general model without blocking chat.

Until then, Qwen3 1.7B Q8 remains the first-run model for chat, code, tools,
JSON, and multilingual use.

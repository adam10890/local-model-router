# Local-LLM landscape: where Imperium stands

**Status:** Review and backlog — no behavior described here is shipped
**Owner:** Product / founder
**Source:** [`PurpleDoubleD/awesome-local-llm`](https://github.com/PurpleDoubleD/awesome-local-llm)
(a fork of `rafska/awesome-local-llm`), read 2026-11-06
**Outcome:** three selected capabilities, each with a PRD under [`prd/`](prd/)

This document maps a curated catalogue of the local-LLM ecosystem onto what
Imperium already does, so that new work is chosen against a known landscape
rather than a hunch. It is a backlog, not a plan of record. Nothing here
competes with the 1.0 beta gates: [`1.0-beta-evidence.md`](1.0-beta-evidence.md)
currently rates six of seven goals Partial, and every proposal below is gated
behind that the way [`future-orchestration.md`](future-orchestration.md) gates
orchestration.

## Source fidelity

Two URLs were read:

- `https://github.com/PurpleDoubleD/awesome-local-llm`
- `https://raw.githubusercontent.com/PurpleDoubleD/awesome-local-llm/main/README.md`

The raw README was captured verbatim through *Tools → Computer Use*. Everything
after that point — browser automation, memory, observability, research,
training, sandboxing, hardware, tutorials, communities — reached this document
as a paraphrased summary. Entries from those sections are directional and
should be re-checked against the source before anyone acts on a specific name.
The catalogue itself is small (15 stars at the time of reading); it is useful
as a survey of what the ecosystem builds, not as an authority on what is best.

## What the catalogue covers

Nine top-level sections: inference platforms, inference engines, user
interfaces, large language models (explorers and benchmarks, providers, and
specific models by purpose), tools, hardware, tutorials, communities,
contributing. The weight sits in two places: a long hardware-specific inference
engine list (NVIDIA, AMD, Intel, Apple MLX, NPUs, distributed) and a long tools
list (agent frameworks, MCP, RAG, coding agents, computer use).

## Gap analysis

Each row was checked against `main` at 0.13.0 in code, not from memory.

| Catalogue area | Representative entries | Imperium today | Gap |
| --- | --- | --- | --- |
| Inference engines | `llama.cpp`, `vllm`, `ollama`, `koboldcpp` | `helpers/backends/` (remote, docker, subprocess) plus `upstreams/registry.py` for any OpenAI-compatible server | none |
| Model swapping | `llama-swap` | `router_mode` slots, and the safe single-GPU swap with bounded drain from ADR 0015 | none |
| Hardware fit | `llmfit`, VRAM calculators | `cookbook/` — GGUF header parsing, VRAM fit math, per-role recommendations, `GET /cookbook` | none |
| Benchmarks | `oobabooga benchmark`, Dubesor table | `evaluation.py`, `imperium evaluate-models`, `GET /routing/evaluations` | none |
| Routing gateways | `archgw`, `Arch-Router-1.5B` | this is Imperium's own surface | none |
| Agent frameworks | `pydantic-ai`, `crewAI`, `agno` | `service/agent_library.py` (pydantic-ai runner), `/agents`, swarm terminals, `execution/` | none |
| MCP | `github-mcp-server`, `playwright-mcp` | `mcp/` server and the A2A card | none |
| Coding agents | `aider`, `continue`, `opencode` | these are *clients*; `harnesses/` pins a model per connection for them | none |
| **Structured output** | `outlines` | `response_format` is read only to detect a routing capability (`routing/catalog.py:170`) and to key the prompt cache; `_forward_payload` (`service/app.py:465`) passes it through untranslated and nothing checks the reply | **selected — PRD 1** |
| **Observability** | `langfuse`, `openllmetry`, `opik` | SQLite `FleetStore` and one bespoke `/routing/analytics`; no `/metrics`, no tracing — a repository-wide search for `prometheus` returns nothing | **selected — PRD 2** |
| **Memory / KV reuse** | `LMCache`, `sglang`, `dynamo` | `service/prompt_cache.py` is exact-match, in-memory and deterministic-only; ranking has no cache affinity | **selected — PRD 3** |
| Retrieval | `graphrag`, `LightRAG`, `haystack`, `Qwen3-Embedding` | `POST /v1/embeddings` routes to a local embed slot; no store, ingestion or retrieval anywhere | deferred — see below |
| Safeguards | `Qwen3Guard`, `Granite Guardian`, `NemoGuard` | `disclosure/` governs what may leave for which executor; nothing inspects inbound content for safety | deferred — see below |
| Speech | `Qwen3-ASR`, `chatterbox`, `VibeVoice` | none | out of scope |
| Computer use, browser automation | `open-interpreter`, `browser-use`, `playwright` | none | out of scope |
| Training and fine-tuning | `OpenRLHF`, `Kiln`, `unsloth` | none | out of scope |
| Model directories | `LLM Explorer`, `bartowski` | `GET /models/directory`, first-run catalogue in `setup/model_catalog.json` | none |

## Selected

Three, each with a PRD. They were chosen for being gateway-shaped — work only
the thing in the middle can do — rather than for being the largest.

1. **[Structured output contract](prd/structured-output.md).** Three backends
   express one intent three ways. Imperium is the only place that can normalize
   it and then check the answer. ADR 0006 calls a wrong answer the intolerable
   daily failure, and malformed JSON to a tool-calling harness is one.
2. **[Observability surface](prd/observability.md).** The counters already
   exist in `FleetStore`; nothing can scrape them. The cheapest of the three and
   the one that makes the other two measurable.
3. **[Cache-aware routing](prd/cache-aware-routing.md).** A warm prefix already
   lives in whichever slot served the last turn, and ranking does not know.
   Pure routing policy over machinery that is already there.

## Considered and deferred

- **Retrieval lane (RAG).** The largest catalogue category Imperium has nothing
  in, and the most visible to a non-technical operator, which makes it the
  strongest candidate for G6 later. Deferred because a corpus store, ingestion
  and chunking push Imperium from a router toward an application, and
  `GOALS.md` is explicit that the router is the product. The router-shaped
  version — a retrieval *lane* that reuses the existing embed slot and injects
  context behind a routable role — is the form to revisit, not a RAG stack.
- **Local guardrail lane.** `Qwen3Guard`, `Granite Guardian` and `NemoGuard`
  are themselves local models, so routing a moderation pass through the fleet
  fits the architecture exactly, and it is the inbound twin of
  [`task-disclosure.md`](task-disclosure.md). Deferred on value, not on fit: a
  solo operator on their own machine gains little from content moderation. It
  becomes worth building if Imperium ever serves someone other than its owner.
- **Speech, computer use, browser automation, fine-tuning.** Each is a product
  in its own right. They belong to clients of the router, not the router.

## Revisiting this

Re-read the source before treating any entry as current; the ecosystem moves
faster than this file. The selection, not the inventory, is the durable part —
and the selection rules in
[`future-improvements.md`](future-improvements.md) still apply: prefer items
that reuse an existing seam, keep local-first contracts intact, and leave
decisions that are taste to the founder.

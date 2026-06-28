# Task 001 — Adopt Human↔AI Doc-Sync

## Description

Introduce the Human↔AI documentation sync system into local-model-router. Human-readable
task docs live under `docs/sync/human/*.md`; their AI-structured counterparts
live under `docs/sync/ai/*.yaml` following the `TaskDefinition` schema. A
dependency-free drift gate (`docs/sync/doc_sync_check.sh`) keeps each pair
aligned.

## Status

In progress — seed pair and drift gate landed.

## Priority

Medium.

## Specifications

- Pairing is by file stem: `human/<id>.md` ↔ `ai/<id>.yaml`.
- The drift gate must run with no Node, no network, no extra deps.
- No agent runtime code is coupled to the sync layer (docs-only feature).

## Dependencies

- Human-AI-doc-sync-tool (canonical bi-directional engine + ROLLOUT_TEMPLATE).

## Metrics

- `doc_sync_check.sh` reports `Status: IN SYNC`.

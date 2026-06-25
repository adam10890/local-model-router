# Doc-Sync Plan — local-model-router

## 0. Pre-flight (per AGENTS.md / CLAUDE.md DOX contract)
- [x] Read AGENTS.md (root) + CLAUDE.md and target paths
- [x] Stack: Python
- [x] Work branch: claude/context-window-optimization-74mwkf
- [x] Confirm: docs/ + non-runtime script only; no Agent Zero imports, no secrets

## 1. Documentation inventory
- [x] Map human docs (README, docs/) -> docs/sync/human/
- [x] AI-YAML candidates: routing runbooks / app profiles / contracts -> docs/sync/ai/
- [x] Seed pair: docs/sync/human/task-001.md + docs/sync/ai/task-001.yaml

## 2. Adapter (Python — stdlib/native, shortest diff)
- [x] Ship docs/sync/doc_sync_check.sh as the local drift gate
- [x] No new runtime deps, no network, behavior-preserving (loopback defaults untouched)

## 3. Sync wiring
- [x] Local dev: run doc_sync_check.sh before commit
- [ ] CI: add a step running `sh docs/sync/doc_sync_check.sh` (follow-up)

## 4. Verification (per CLAUDE.md)
- [x] Docs-only change -> git diff --check clean
- [x] sh docs/sync/doc_sync_check.sh -> "Status: IN SYNC"
- [x] No Python touched -> py_compile / pytest not required for this change

## 5. Handoff
- Branch: claude/context-window-optimization-74mwkf (claude scratch; classify to dev/ or ready/ before merge)
- Files: docs/sync/{PLAN.md,doc_sync_check.sh,human/task-001.md,ai/task-001.yaml}
- Verification: `git diff --check`; `sh docs/sync/doc_sync_check.sh`
- Follow-up: wire gate into CI; expand seeds to real routing runbooks.

# Compute Monitor Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore service startup and ship accurate, non-blocking hardware telemetry through `/fleet/status` with complete tests and release documentation.

**Architecture:** Keep hardware collection in a small synchronous helper backed by `nvidia-smi` and `psutil`. The Starlette service offloads collection with `asyncio.to_thread`, caches snapshots briefly per app, preserves the existing `vram` response, and degrades safely when monitoring fails.

**Tech Stack:** Python 3.10+, Starlette, psutil, pytest, GitHub CLI.

---

### Task 1: Define the hardware-monitor contract with failing tests

**Files:**
- Create: `tests/test_compute_monitor.py`
- Replace: `local_model_router/helpers/compute_monitor.py`

- [ ] **Step 1: Write tests for GPU parsing and graceful failure**

Use a fake `subprocess.run` result containing a valid GPU row and a malformed
row. Assert that the valid row becomes `GPUStats` and the malformed row is
ignored. Add a `FileNotFoundError` case that returns an empty tuple.

- [ ] **Step 2: Write tests for CPU/RAM units and serialization**

Inject a fake psutil object whose memory values are exact MiB multiples and
whose `cpu_percent()` returns `37.5`. Assert MiB values, utilization, Unix
timestamp, JSON-safe `to_dict()`, and the backward-compatible GB VRAM summary.

- [ ] **Step 3: Run the new tests and verify RED**

Run: `python -m pytest tests/test_compute_monitor.py -q`

Expected: collection or assertion failures because the current module lacks
the specified injectable API and crashes on `os.currentTimeMillis()`.

- [ ] **Step 4: Replace the monitor with the minimum implementation**

Implement frozen `GPUStats`, `CPUStats`, and `ComputeSnapshot` dataclasses;
`query_gpus(run=subprocess.run)`; `query_cpu(psutil_module=psutil)`;
`scan_hardware()` using `time.time()`; `to_dict()`; and `vram_summary()`.
Remove slot collection, unused imports, the duplicate `snapshot()` alias, and
all Linux fake-memory fallbacks.

- [ ] **Step 5: Run the monitor tests and verify GREEN**

Run: `python -m pytest tests/test_compute_monitor.py -q`

Expected: all tests pass without invoking real hardware.

### Task 2: Integrate telemetry into the service with failing tests

**Files:**
- Modify: `tests/test_fleet_manager.py`
- Modify: `local_model_router/service/app.py`

- [ ] **Step 1: Add integration tests**

Monkeypatch `scan_hardware` with a deterministic snapshot. Assert `/health`
returns the service name, `/fleet/status` returns the existing `vram` keys and
the additive `compute` block, and two immediate requests invoke the scanner
once. Add a scanner exception test asserting a 200 response, unknown VRAM,
and `compute.available == false`.

- [ ] **Step 2: Run the service tests and verify RED**

Run: `python -m pytest tests/test_fleet_manager.py -q`

Expected: import failure for missing `to_gb` before implementation.

- [ ] **Step 3: Implement non-blocking cached integration**

Restore `_SERVICE_NAME` and `vram_unknown_summary`; import `asyncio` and the
snapshot type; keep a five-second monotonic cache inside `create_app`; call
the scanner through `await asyncio.to_thread(scan_hardware)`; and catch probe
exceptions without failing the endpoint.

- [ ] **Step 4: Run service and monitor tests and verify GREEN**

Run: `python -m pytest tests/test_compute_monitor.py tests/test_fleet_manager.py tests/test_fleet_control.py -q`

Expected: all tests pass.

### Task 3: Complete packaging and release governance

**Files:**
- Modify: `pyproject.toml`
- Modify: `local_model_router/__init__.py`
- Create: `RELEASE.md`
- Create: `CHANGELOG.md`
- Create: `changes/4-compute-monitor.md`
- Modify: `docs/PROVIDER.md`
- Modify: `README.md`

- [ ] **Step 1: Declare runtime and version metadata**

Add `psutil>=5.9` to core dependencies and change the package version from
`0.2.0` to `0.3.0` for the additive API feature.

- [ ] **Step 2: Document release policy and user-visible behavior**

Create a concise release checklist covering version, changelog, docs, release
fragment, compile, full pytest, branch, and clean staged scope. Add an
Unreleased changelog entry and a matching release fragment. Document the
`vram` and `compute` response blocks plus best-effort behavior in the provider
runbook and mention live hardware telemetry in the README feature table.

- [ ] **Step 3: Verify documentation and packaging**

Run: `python -m pip install -e .`

Run: `git diff --check`

Expected: editable install succeeds and the diff has no whitespace errors.

### Task 4: Verify and publish

**Files:**
- Verify all changed files.

- [ ] **Step 1: Compile touched Python files**

Run: `python -m py_compile local_model_router/helpers/compute_monitor.py local_model_router/service/app.py local_model_router/__init__.py tests/test_compute_monitor.py tests/test_fleet_manager.py`

- [ ] **Step 2: Run the full hermetic suite**

Run: `python -m pytest tests/ -q`

Expected: zero failures and zero collection errors without a live fleet or GPU.

- [ ] **Step 3: Audit scope and release checklist**

Run: `git status --short`, `git diff --check`, and inspect `git diff --stat origin/main...HEAD` plus staged changes. Confirm no machine-local config or secrets.

- [ ] **Step 4: Commit and publish**

Commit only intended files, rename the verified branch to
`ready/compute-monitor-fixes`, push it, create a ready PR against `main`, wait
for checks, and merge with `gh pr merge --squash --delete-branch`.

- [ ] **Step 5: Verify merged main**

Fetch `origin/main`, verify the merge commit contains the expected files, and
run the full suite against the merged result before reporting completion.

# Imperium Goals

**Status:** Active product contract
**Current milestone:** 1.0 beta readiness
**Last reviewed:** 2026-08-02
**Owner:** Product/founder

This file defines the outcomes Imperium must achieve. It is intentionally
shorter and more stable than the implementation roadmap.

- [`AGENTS.md`](AGENTS.md) defines engineering contracts and invariants.
- [`README.md`](README.md) describes behavior available today.
- [`docs/1.0-beta-roadmap.html`](docs/1.0-beta-roadmap.html) sequences accepted
  product work.
- [`RELEASE.md`](RELEASE.md) defines the merge and release gates.

## Mission

Make private local AI dependable and understandable for one operator: one
Windows-first application, one stable OpenAI-compatible gateway, and clear
control over models, routing, capacity, failures, and recovery.

Imperium is the product. Agent Zero, Hermes, Pi, Claude Code, and other
runtimes are clients of the same router contract.

## Primary user

The primary user is a solo operator who wants useful local AI without having
to understand Docker, llama.cpp flags, YAML, Python environments, or routing
internals. Advanced operators may opt into those controls, but the default
path must not require them.

## North-star outcome

On a clean supported Windows system, a new user can install Imperium, reach a
successful local chat completion, connect at least one supported client, and
recover from common failures through guided actions without editing code or
YAML.

## Product goals

| ID | Priority | Required outcome | Evidence of success |
| --- | --- | --- | --- |
| G1 | P0 | **Guided first success.** Installation, model discovery or approved bootstrap, startup, and the first local completion work without external Python, mandatory Docker, or manual YAML. | Clean-machine checks on supported NVIDIA, AMD, Intel, and CPU-only paths; packaged first-run smoke. |
| G2 | P0 | **Stable client compatibility.** The OpenAI-compatible API and every declared harness profile have versioned, reproducible setup instructions. Configuration is checked against the client's official repository and both the installed and current stable versions. | Hermetic API tests plus real `/health`, `/models`, chat, stream, and tool-call smoke where the client supports them. |
| G3 | P0 | **Reliable, explainable routing.** Healthy local capacity wins by default; capability checks, admission, failover, and upstream use produce explicit reasons. A pinned harness never silently changes its model. | Full hermetic suite, deterministic routing assertions, and live llama.cpp/provider smoke. Zero unexplained fallbacks. |
| G4 | P0 | **Local-first privacy and safe control.** Prompt bodies and secrets never enter telemetry or previews; public binds require authentication; cloud/upstream routing and fleet mutation remain explicit opt-ins. | Security and redaction tests. Zero prompt bodies or secrets in persisted telemetry. Public no-auth startup is rejected unless explicitly acknowledged. |
| G5 | P0 | **Recoverable operation.** Setup, start, restart, update, repair, rollback, and uninstall fail safely and provide one actionable next step. Imperium never stops an unverified process or overwrites local configuration without review and backup. | Release lifecycle checks, failure-code assertions, backup/rollback tests, and clean-machine recovery walkthroughs. |
| G6 | P1 | **Understandable daily use.** Home, Chat, Models, Connections, and status surfaces explain what is ready, what failed, and what to do next without requiring raw logs. English/Hebrew, LTR/RTL, light/dark, keyboard, and responsive basics remain usable. | Browser smoke plus a manual walkthrough of success, empty, loading, degraded, and error states. |
| G7 | P1 | **Sustainable solo-owner delivery.** `main` stays shippable; changes remain small, tested, documented, and reversible; optional packages cannot prevent the router core from starting. Dependencies are added only when they remove more complexity than they introduce. | Release checklist, clean branch inventory, full tests without fleet/GPU/network, and documented rollback for release-affecting changes. |

## 1.0 beta exit gates

The beta is ready only when all P0 goals pass and there is recorded evidence
for each of these gates:

1. A clean Windows installation reaches a local completion without manual
   configuration editing.
2. Source and packaged builds pass their hermetic and live smoke checks.
3. Supported client and harness setup is versioned and reproducible.
4. The dashboard communicates readiness and recovery without raw-log
   dependency.
5. Update, repair, rollback, and uninstall have been exercised on a release
   candidate.
6. Security invariants have zero known violations.
7. Version, changelog, documentation, limitations, tests, and release fragments
   agree with the shipped behavior.

## Non-goals for 1.0 beta

- Rebuilding Imperium as an Agent Zero plugin or adding client-specific imports
  to the router core.
- Mandatory Docker orchestration or cloud routing by default.
- A full LangFlow-style visual workflow editor.
- A plugin or agent marketplace.
- Fine-tuning, model training, or a general-purpose model marketplace.
- Cloud BYOK as a core product surface.
- Advanced worker orchestration before the re-entry gates in
  [`docs/future-orchestration.md`](docs/future-orchestration.md) pass.

## Decision order

When goals compete, decide in this order:

1. Privacy and safety.
2. First-run success and recovery.
3. Routing correctness and client compatibility.
4. Operational clarity.
5. New capability.

Until the 1.0 beta gates pass, stability and release integrity take precedence
over major new features.

## Audit protocol

An audit against this file must evaluate each goal as **Pass**, **Partial**,
**Fail**, or **Unknown** and cite repository, test, release, or runtime evidence.
Missing evidence is `Unknown`, not `Pass`. P0 failures block beta readiness.

An audit is read-only unless fixes are separately requested. Changing the
mission, primary user, priorities, or non-goals requires an explicit product
decision; implementation documents do not override this contract.

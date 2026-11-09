# Herdr-inspired development TODO

**Updated:** 2026-11-08. **Implementation:** not started by this documentation change.
**Owner:** Imperium maintainers. **Planning authority:** Adam requested this backlog/work plan; implementation selection remains explicit.
**Read first:** [design/source inventory](herdr-development.md) · [priority, effort and dependencies](herdr-work-plan.md).

This is a repository development checklist, not Personal Dashboard commitments or a second operational task store. All 60 R IDs have exactly one primary package owner below. An unchecked item is not implementation authorization. States: **candidate**, **selected** (explicit scope/owner recorded), **in progress**, **blocked** (specific dependency), **deferred** (gate/not selected), **verified** (linked evidence), or **rejected** (reason retained). No package is marked selected or complete merely because these docs exist.

## Next proposed slices

Recommended review order: **P01 counter regression → P03 honest readiness → P02 draft/focus → P05 stop cause → P10 recovery gaps**. These can be implemented independently in isolated slices; P01 does not wait for a journal, native app, global framework or Herdr trial. Reorder a concrete security/configuration defect ahead of UI polish if one is found.

Each package section records its primary proposal IDs, delivery state, first bounded implementation and measurable completion. Future checks named here are acceptance work, not tests run by this documentation change.

<a id="p01"></a>
## P01 - Correct setup event cursors and resync

- **State:** candidate - gap review and selection required. **Proposals:** R16 R17 R18. **Effort:** Short fix; Medium full resync; **simplicity:** Moderate; **urgency/value:** U0 / Very high.
- **Depends on:** None. **Gate:** Existing-path fix now; review additive replay contract separately.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Use an independent monotonic counter; distinguish retained, lost and unavailable history; define snapshot/startup ordering without introducing a generic event framework.
- [ ] Verify: After retention rolls over, later events remain discoverable. Test the exact retained boundary and a transition during subscription startup; a gap requests resync rather than returning a misleading empty list.
- **Code/check owners:** local_model_router/setup/engine.py; tests/test_setup_engine.py; tests/test_setup_api.py.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p02"></a>
## P02 - Preserve chat continuity and consume existing streaming

- **State:** candidate - gap review and selection required. **Proposals:** R02. **Effort:** Medium; **simplicity:** Moderate; **urgency/value:** U0 / Very high.
- **Depends on:** Existing gateway streaming; no Workbench storage dependency. **Gate:** Draft/focus maintenance now; streaming/queue expansion follows existing post-beta Chat V2 gate.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Preserve draft/caret/focus/scroll across updates, then render partial answers through the existing streaming contract. Avoid a frontend rewrite.
- [ ] Verify: Type a draft, trigger theme/language/readiness updates and keep it intact. For the gated streaming slice, partial output survives interruption and a reconnect never resubmits automatically.
- **Code/check owners:** local_model_router/dashboard/index.html; tests/test_dashboard_static_contract.py; tests_e2e/dashboard/test_dashboard.py; tests/test_openai_chat_completions.py.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p03"></a>
## P03 - Make readiness fresh and reject obsolete results

- **State:** candidate - gap review and selection required. **Proposals:** R19 R20 R21. **Effort:** Medium; **simplicity:** Moderate; **urgency/value:** U0 / Very high.
- **Depends on:** Existing readiness and connection evidence. **Gate:** Existing-path maintenance now.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Add last-success/freshness and generation fences; revalidate on visibility/wake. Surface configured, reachable, completion-verified and client-verified evidence accurately.
- [ ] Verify: After an online check, disconnect and show stale/unavailable. A late older reply cannot restore ready. A models-list response alone does not establish client completion success.
- **Code/check owners:** local_model_router/service/readiness.py; local_model_router/harnesses/setup.py; local_model_router/dashboard/index.html; tests/test_harness_api.py; tests_e2e/dashboard/test_dashboard.py.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p04"></a>
## P04 - Bound retries and deadlines independently per target

- **State:** candidate - gap review and selection required. **Proposals:** R22 R23 R33. **Effort:** Medium; **simplicity:** Moderate; **urgency/value:** U1 / High.
- **Depends on:** A demonstrated current reconnect/setup path; P03 generation helper may be shared. **Gate:** Gap review first; no second inference scheduler.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Classify transient versus repair-required failures, keep target retries independent, reset delay after stable readiness and share one remaining deadline across phases.
- [ ] Verify: One failed target leaves others usable. Authentication/configuration produces one repair action. Multi-phase checks stay within one deadline and do not create duplicate concurrent attempts.
- **Code/check owners:** local_model_router/service/readiness.py; local_model_router/harnesses/setup.py; existing provider/setup tests selected during gap review.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p05"></a>
## P05 - Explain stops and distinguish disconnect from stopped

- **State:** candidate - gap review and selection required. **Proposals:** R34 R46. **Effort:** Short attribution; Medium cancellation reconciliation; **simplicity:** Moderate; **urgency/value:** U0 / Very high.
- **Depends on:** Existing lifecycle ownership and request IDs. **Gate:** Attribution now; durable job cancellation extends it later.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Record the first stop cause and safe initiating actor/request. Separate cancel requested, transport lost, stopped confirmed and outcome unknown.
- [ ] Verify: A second stop cannot overwrite the first recorded cause. A disconnected browser cannot mark a still-running operation stopped. Exclude prompts, secret-bearing arguments and terminal text.
- **Code/check owners:** local_model_router/cli.py; local_model_router/setup/engine.py; local_model_router/service/app.py; tests/test_cli_commands.py; tests/test_setup_engine.py.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p06"></a>
## P06 - Preserve valid configuration and personal customizations

- **State:** candidate - gap review and selection required. **Proposals:** R13 R14 R28. **Effort:** Medium; **simplicity:** Moderate; **urgency/value:** U1 / High.
- **Depends on:** Inventory existing reload and persistence behavior. **Gate:** Extend current configuration helpers only.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Keep last usable runtime state on invalid reload, preserve unread/newer data before saving, and distinguish shipped-default fingerprints from personal edits.
- [ ] Verify: Malformed/newer input is retained with an actionable error; it is not overwritten with empty defaults. Updating a template preserves unrelated edits and offers a reviewable change.
- **Code/check owners:** local_model_router/harnesses/profiles.py; local_model_router/setup/engine.py; tests/test_harness_profiles.py.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p07"></a>
## P07 - Protect user-owned configuration publication

- **State:** candidate - gap review and selection required. **Proposals:** R39. **Effort:** Short to Medium; **simplicity:** Moderate; **urgency/value:** U1 / High.
- **Depends on:** Existing configuration writer and native Windows fixtures. **Gate:** Move ahead if gap review exposes a concrete safety defect.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Add only missing ownership, link, permission and private-staging checks around the established publication path.
- [ ] Verify: Read-only, symlink/hardlink and failed-replacement cases preserve the prior configuration and fail honestly. Do not claim a transaction across multiple files.
- **Code/check owners:** local_model_router/harnesses/profiles.py; tests/test_harness_profiles.py; targeted Windows configuration cases.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p08"></a>
## P08 - Install scoped client hooks safely

- **State:** candidate - gap review and selection required. **Proposals:** R38 R40 R41. **Effort:** Medium for the first client; **simplicity:** Moderate; **urgency/value:** U1 / High.
- **Depends on:** Selected client and verified current official hook contract; P07 where config is written. **Gate:** First supported client only; no installation authorized by this document.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Preserve native hook groups, dedupe exact managed commands, decode UTF-8 stdin independently of the console, and bind reports to the proper parent/thread.
- [ ] Verify: Two installations do not duplicate hooks or alter unrelated entries. Hebrew input survives a non-UTF-8 console code page. Nested/wrong-client reports are rejected without transcript ingestion.
- **Code/check owners:** local_model_router/harnesses/setup.py; local_model_router/harnesses/profiles.py; tests/test_harness_profiles.py; new isolated client-hook tests.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p09"></a>
## P09 - Version supported-client compatibility evidence

- **State:** candidate - gap review and selection required. **Proposals:** R36 R37 R42. **Effort:** Medium for the first client; **simplicity:** Moderate; **urgency/value:** U1 / Very high.
- **Depends on:** Supported-client and installed/stable-version inventory. **Gate:** Schema fixtures now; real-client evidence remains a separate gate.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Declare supported capabilities/version floors and freeze promised request, response and stream fixtures. Keep version lookup failure Unknown.
- [ ] Verify: Older supported fixtures still decode/behave correctly; additive fields are tolerated where promised. Exact dedicated-route canary proves gateway use before client verification is reported.
- **Code/check owners:** docs/HARNESSES.md; docs/spynel-adoption-plan.md; tests/test_harness_api.py; tests/test_smoke_harnesses.py; tests/test_openai_chat_completions.py.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p10"></a>
## P10 - Exercise the actual Windows installer recovery path

- **State:** candidate - gap review and selection required. **Proposals:** R47 R48 R51. **Effort:** Medium; **simplicity:** Moderate; **urgency/value:** U1 / Very high.
- **Depends on:** Existing Windows cleanroom harness and disposable fixture directories. **Gate:** Add missing cases rather than a second installer.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Inject promotion and rollback failures, preserve recoverable backups and literal PATH/registry types, and test the entrypoint actually shipped.
- [ ] Verify: Failed promotion restores the old install; failed rollback preserves its backup and reports the real recovery path. Raw PATH representation and unrelated entries remain intact.
- **Code/check owners:** scripts/test_windows_cleanroom.ps1; tests/test_windows_packaging.py; Windows installer/distribution entrypoints selected in gap review.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p11"></a>
## P11 - Verify staged updates and interrupted-update recovery

- **State:** candidate - gap review and selection required. **Proposals:** R49 R50. **Effort:** Long until lifecycle gaps are bounded; **simplicity:** Complex; **urgency/value:** U1 / High.
- **Depends on:** Packaged updater trust/lifecycle review; P10 fixture machinery is reusable. **Gate:** Reduce scope after reviewing existing updater; no redundant updater.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Publish a ready receipt only after archive and executable-version checks, recover interrupted staging and guard relaunch loops while respecting active requests.
- [ ] Verify: Corrupt/unexpected-version candidates never become ready. Interruption does not destroy the prior build or relaunch indefinitely. Same-origin checksum is not described as an independent signature.
- **Code/check owners:** local_model_router/setup/engine.py; scripts/test_windows_cleanroom.ps1; tests/test_setup_engine.py; tests/test_windows_packaging.py.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p12"></a>
## P12 - Make existing actions discoverable and strictly targeted

- **State:** candidate - gap review and selection required. **Proposals:** R08 R09 R10 R54. **Effort:** Medium; Short unknown-argument slice; **simplicity:** Moderate; **urgency/value:** U1 / High.
- **Depends on:** Current action inventory; no plugin framework. **Gate:** Existing actions only; palette is additive.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Use a small shared action registry for keys/palette/help/examples. Reject unknown arguments before resolving a target; add Hebrew search and keyboard support.
- [ ] Verify: A misspelled target fails before mutation. Palette keyboard/focus behavior works in Hebrew/RTL. Generated examples refer to real available commands and do not grant mutation authority.
- **Code/check owners:** local_model_router/dashboard/index.html; local_model_router/cli.py; tests/test_cli_commands.py; tests_e2e/dashboard/test_dashboard.py.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p13"></a>
## P13 - Expose inspectable content-free diagnostics

- **State:** candidate - gap review and selection required. **Proposals:** R60. **Effort:** Short to Medium; **simplicity:** Simple to Moderate; **urgency/value:** U1 / High.
- **Depends on:** Existing diagnostics and telemetry allowlist. **Gate:** No external telemetry or installation identifier.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Show the exact operational fields and export scope; retain the current redaction boundary and useful privacy tests.
- [ ] Verify: The operator can inspect the report. Synthetic secrets, prompt/result bodies and terminal text stay absent from telemetry, notifications and generic diagnostic export.
- **Code/check owners:** local_model_router/diagnostics.py; tests/test_diagnostics.py; disclosure/auth tests for affected boundaries.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p14"></a>
## P14 - Track selected-source provenance and notice freshness

- **State:** candidate - gap review and selection required. **Proposals:** R53. **Effort:** Short for the first selected units; **simplicity:** Simple; **urgency/value:** U1 when copying / High.
- **Depends on:** Selected copied/adapted units; independent of unrelated fixes. **Gate:** Required before redistributing actual borrowed material.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Record upstream SHA/path, destination, modifications, applicable licenses/notices and upgrade checks for the narrow selected material.
- [ ] Verify: Redistributed artifacts contain the selected material's required notices. A changed upstream unit/dependency prompts deliberate review; unrelated asset licenses are not silently assumed Apache.
- **Code/check owners:** THIRD_PARTY_NOTICES.md; licenses/; pyproject.toml; packaging checks for actual selected files.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p15"></a>
## P15 - Add the private durable Workbench journal and artifacts

- **State:** candidate - Workbench gate/decision required. **Proposals:** R27 R29 R30. **Effort:** Long; **simplicity:** Complex; **urgency/value:** U2 / Very high.
- **Depends on:** WB0 approved boundary/storage contract and post-beta entry gate. **Gate:** New private store; content-free fleet telemetry stays separate.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Implement stable conversation/run/attempt/event/artifact identities, atomic publication, explicit saved/interrupted states and a bounded retention contract.
- [ ] Verify: Reload/restart attaches to the same run without duplicate dispatch. Stale attempts cannot finalize. A crash between file and manifest publication cannot expose a ready broken artifact; persisted-write failure is not success.
- **Code/check owners:** New optional Workbench store and dedicated crash/replay/privacy tests; existing FleetStore is not the private content store.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p16"></a>
## P16 - Make edits and uncertain mutations recoverable

- **State:** candidate - Workbench gate/decision required. **Proposals:** R15 R32 R35. **Effort:** Long for complete action semantics; **simplicity:** Complex; **urgency/value:** U2 / Very high.
- **Depends on:** P15 durable intents/receipts; canonical-service contract agreed in WB0; integrate with WB2 for canonical application. **Gate:** Read-only editor slice can precede task mutations.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Retain drafts on stale edits; bind writes to revisions; use stable action keys and reconciliation for unknown outcomes rather than blind retry.
- [ ] Verify: Concurrent/stale edits never silently overwrite. Same-key replay returns the original outcome or rejects changed payload. A crash/timeout after apply reconciles the authoritative receipt without duplicate effect.
- **Code/check owners:** New Workbench editor/action tests plus canonical task-service concurrency and receipt tests.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p17"></a>
## P17 - Bound streaming and keep execution responsive

- **State:** candidate - Workbench gate/decision required. **Proposals:** R24 R25 R26. **Effort:** Medium to Long after selecting the path; **simplicity:** Complex; **urgency/value:** U2 / High.
- **Depends on:** Representative producer/consumer path; P15 when used for durable work. **Gate:** Reuse existing limits; no generic scheduler.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Bound event/stream fanout by entries and bytes, keep slow writes isolated, and test cancellation/flush/fairness under a stalled consumer.
- [ ] Verify: Memory stays bounded under a stalled reader; ordered state mutations cannot drop silently; cancellation and other clients remain responsive. Foreground inference respects existing admission.
- **Code/check owners:** Existing gateway/admission tests and new bounded Workbench event/worker tests.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p18"></a>
## P18 - Build central chat with contextual artifact review

- **State:** candidate - Workbench gate/decision required. **Proposals:** R01 R03 R11 R12. **Effort:** Long for a complete saved flow; **simplicity:** Moderate to Complex; **urgency/value:** U2 / Very high.
- **Depends on:** P02 stable chat; P15 saved artifacts; WB1 for real research outputs. **Gate:** Mocked layout may precede persistence; apply actions need P16/WB2.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Add peek/keep, bounded private-root previews, source-linked artifact display and review diffs within the existing browser stack.
- [ ] Verify: An operator reads/reopens a saved report beside the same conversation. Draft/focus survive; keyboard/RTL/error states work. Out-of-root, binary, huge and unsafe rendered content are handled explicitly.
- **Code/check owners:** local_model_router/dashboard/; tests_e2e/dashboard/test_dashboard.py; new private-artifact API/rendering tests.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p19"></a>
## P19 - Show an authoritative attention board

- **State:** candidate - Workbench gate/decision required. **Proposals:** R04 R05 R06 R07 R31. **Effort:** Medium to Long; **simplicity:** Moderate; **urgency/value:** U2 / High.
- **Depends on:** P15 authoritative run/event records; P18 integration is useful. **Gate:** Start with blocked/unseen; defer grouping polish until multi-job demand.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Display real run states, unseen results and required actions; carry source/sequence/expiry and add pin/group/quiet presentation only when useful.
- [ ] Verify: Seen, agent-reported done, artifact saved and verified task success remain distinct. Stale reports expire. Hiding/pinning a card never changes canonical task ownership or execution state.
- **Code/check owners:** New Workbench run/attention tests and dashboard browser tests.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p20"></a>
## P20 - Measure repeatable UI and Workbench workloads

- **State:** candidate - Workbench gate/decision required. **Proposals:** R55. **Effort:** Short to Medium; **simplicity:** Moderate; **urgency/value:** U2 / Medium.
- **Depends on:** A representative runnable UI seam; baseline may precede major UI work. **Gate:** Use synthetic/local fixtures first; no GPU/model trial required.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Record idle, long-answer, event-burst and reconnect workloads with comparable settings and artifacts; avoid upstream process-control scripts.
- [ ] Verify: Before/after runs use the same workload, activity and display assumptions. Report responsiveness/process resources honestly; render calls are not physical FPS or model throughput.
- **Code/check owners:** tests_e2e/dashboard/; new bounded local workload fixtures only where needed.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p21"></a>
## P21 - Define optional execution lifecycle and observed status

- **State:** deferred - post-gate optional execution. **Proposals:** R43 R44 R45 R56. **Effort:** Long; **simplicity:** Complex; **urgency/value:** U3 / Conditional high.
- **Depends on:** WB0 execution decision plus every future-orchestration re-entry gate. **Gate:** Optional adapter; core starts without it.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Specify visible/stoppable/capacity-bounded workers, launch readiness, transition-aware waits, status provenance and detach/exit semantics.
- [ ] Verify: Workers are visible and stoppable; new requests cannot match an old done turn. Inferred status is labeled and never verifies task success. Model requests prove use of approved Imperium profiles.
- **Code/check owners:** docs/future-orchestration.md; local_model_router/service/swarm_gate.py; tests/test_swarm_gate.py; new adapter lifecycle tests.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p22"></a>
## P22 - Resume one supported client safely

- **State:** deferred - post-gate optional execution. **Proposals:** R57. **Effort:** Medium to Long for the first client; **simplicity:** Complex; **urgency/value:** U3 / Medium.
- **Depends on:** P21 verified lifecycle and session identity. **Gate:** Optional; no auto-resume of arbitrary shell commands.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Use typed session references, bounded native argv and dedupe; distinguish restored layout, resumed client and verified task recovery.
- [ ] Verify: Real Windows resume smoke confirms the chosen client/session, no duplicate launch and explicit failure. POSIX quoting restrictions are not treated as Windows-safe execution.
- **Code/check owners:** Optional execution adapter and new native Windows supported-client resume tests.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p23"></a>
## P23 - Isolate coding activities in explicit worktrees

- **State:** deferred - post-gate optional execution. **Proposals:** R58. **Effort:** Medium; **simplicity:** Moderate; **urgency/value:** U3 / Medium.
- **Depends on:** P21 selected coding executor and explicit workspace mutation authority. **Gate:** Existing manual isolation rules already apply now.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Name root/branch/owner per activity and preserve dirty work; exclude recursive forced-cleanup recovery.
- [ ] Verify: The canonical checkout and unrelated edits remain unchanged. Dirty work cannot be silently deleted, reset, stashed or moved; workspace cleanup requires its own verified scope.
- **Code/check owners:** docs/development/git-workflow.md; optional adapter and hermetic Git fixture tests.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

<a id="p24"></a>
## P24 - Make remote execution and local control explicit

- **State:** deferred - post-gate optional execution. **Proposals:** R52 R59. **Effort:** Long; **simplicity:** Complex; **urgency/value:** U3 / Medium.
- **Depends on:** P21 and a demonstrated remote/local-IPC need. **Gate:** Existing authenticated HTTP preferred before new IPC.
- [ ] Recheck exact affected baseline and existing coverage; record selected scope, owner and missing behavior before implementation.
- [ ] Show actual machine/location, independent reconnect and disclosure eligibility; add IPC ownership/elevation checks only if IPC is selected.
- [ ] Verify: Remote reachability is not disclosure approval. Wrong targets/integrity/ownership are rejected. Local-only content cannot leave through the adapter; absence of it cannot block core startup.
- **Code/check owners:** docs/task-disclosure.md; optional adapter tests; native Windows IPC tests only if chosen.
- **Evidence:** no implementation PR, runtime proof or completion receipt recorded.

## Companion Workbench prerequisites

### WB0 - approved boundary and storage/action design

**State:** candidate; product/ADR reconciliation required. **Effort:** Medium; complex decision work. **Depends on:** existing contracts and selected product scope.

- [ ] Specify private conversation/run/attempt/event/source/artifact/action records, stable keys, retention/export/clear scope, restart/cancellation and authority boundaries.
- [ ] Reconcile with GOALS, existing post-beta Chat V2 and HIVE/general-task ownership; draft narrow ADR(s) where material and leave Proposed until the designated decision.
- [ ] Record bounded source-network/model-disclosure choices and measurable initial acceptance limits. No new Accepted decision is implied by this TODO.

### WB1 - bounded research to a saved source-linked report

**State:** candidate, blocked for implementation until WB0/P15 and existing entry gates. **Effort:** Long. **Depends on:** approved private run/artifact contract and gateway/admission.

- [ ] Start with selected local/text sources and explicit scope; public-web retrieval is a separately selected mode. Bound concurrency, work, source bytes and cancellation.
- [ ] Distinguish a search hit, a read source, failed retrieval and cited evidence; save immutable source/artifact manifests with digests and origin links.
- [ ] Verify reload/restart/partial output, source quality/citation checks, privacy, latency and foreground admission on declared synthetic fixtures before daily use.

### WB2 - canonical task projection and reviewed apply receipts

**State:** candidate; blocked for writes until owning-service contract/evidence exists. **Effort:** Long; external effort Unknown. **Depends on:** WB0, P15/P16, WB1 artifact and canonical service.

- [ ] Obtain narrow authorized reads and the canonical service's atomic revision, dedupe, receipt and outcome-lookup contract. Do not create a parallel task store.
- [ ] Bind an exact before/after preview to owner, target, expected revision, payload digest and immutable artifact version; invalidate stale previews.
- [ ] Prove same-key replay, concurrent change, timeout after apply, crash/restart and outcome-unknown reconciliation without duplicate effect. Show confirmed board update separately from report saved or action approved.

## Optional product-learning trial T01

**State:** optional discovery, not installed/scheduled by this change. **Effort:** Short. No package depends on it.

- [ ] After separate installation authorization, try one real client task, parallel attention/review, and window close/reopen in a disposable project.
- [ ] Record at most three observed benefits, friction and specific candidate R IDs; label source claims versus observed UI behavior.
- [ ] Preserve existing Imperium installation/config, disable optional usage reporting, avoid unreviewed mods and verify actual gateway use separately. Stop if setup cost or extra window management outweighs the observed value.

## Completion and release checklist for a selected implementation slice

- [ ] Record selected package/sub-slice, approver/owner, exact baseline, branch, files, dependencies and unresolved risks; preserve other workers' changes.
- [ ] Follow the owning child DOX and RELEASE: appropriate versioning, changelog, release fragment, docs, touched-file compilation, hermetic tests and relevant native/browser/client checks.
- [ ] Apply P14 only if material is copied/adapted; retain provenance/notices and selected-unit upgrade instructions. Independent implementation does not require an upstream runtime.
- [ ] Keep local checks, CI, package recovery, live/client evidence and actual deployment distinct. No passing label without the promised evidence.
- [ ] Link the implementation/verification evidence before changing a package or sub-slice to verified; integration/merge remains Adam's gate.

For this docs-only change: version bump/changelog/release fragment are not applicable; validate whitespace, local links, R coverage, package/companion dependencies and the unchanged active checkout. No runtime tests are implied.

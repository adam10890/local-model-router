# Herdr-inspired development design

**Status:** Planning requested by Adam on 2026-11-08; implementation not started by this change.
**Owner:** Imperium maintainers; product/core-contract decisions remain with Adam.
**Evidence:** Herdr source review on 2026-11-07; local Imperium baseline rechecked on 2026-11-08.
**Companions:** [TODO](herdr-todo.md) · [work plan and estimates](herdr-work-plan.md).

## Outcome and authority

Strengthen dependable local AI and a coherent conversation-to-reviewed-work journey by selectively adapting Herdr patterns. Preserve local autonomy, visible execution and human control of effects. A mandatory terminal runtime, hidden execution authority or a competing task database would undermine that purpose.

This document records proposed design and research, not shipped behavior or blanket implementation approval. Adam explicitly requested development documentation, a TODO and a work plan. Existing product/architecture gates still apply when a package is selected. The [beta roadmap](1.0-beta-roadmap.html) remains the owner of accepted sequencing; [GOALS](../GOALS.md), [ADR 0003](adr/0003-simple-then-advanced.md), [ADR 0005](adr/0005-hermes-decompose-imperium-server-docs.md), [ADR 0007](adr/0007-solo-tool-approval-required.md) and [future orchestration](future-orchestration.md) remain governing contracts.

The recommended first slice fixes demonstrated existing-path defects. Chat V2 expansion stays after the current beta gates; optional execution stays after the orchestration re-entry gates. The selected central-chat/board/artifact direction is retained as Workbench design, with contract reconciliation explicitly listed as WB0 rather than silently changing GOALS here.

## Baseline and existing seams

The original planning baseline was `c0f1865f90388265c04d226dd5ee52106ee8beee` on `dev/herdr-development-plan`; the documentation review branch is `ready/herdr-development-plan`. The active checkout's pre-existing edits were excluded. Before publication on 2026-11-08, GitHub main was verified at the same source baseline. Source review and documentation checks do not establish CI, packaged or installed-runtime results.

| Existing owner | Reuse | Remaining gap / limit |
| --- | --- | --- |
| [Dashboard](../local_model_router/dashboard/index.html) | Home/Chat/Models/Connections, Hebrew/RTL/theme, stop/retry and current browser transcript | Draft/focus continuity, honest freshness and consumption of existing gateway streaming; one browser transcript is not durable Workbench sessions. |
| [Setup engine](../local_model_router/setup/engine.py) | Managed runtime lifecycle, cancellation and setup progress | Event IDs use retained-list length, then retain 200 records. A method-level synthetic reproduction on 2026-11-07 produced repeated ID 201 and no later rows for cursor 201; the same method bodies remain in this baseline. No real installation failure is asserted. |
| [Readiness](../local_model_router/service/readiness.py), [harness setup](../local_model_router/harnesses/setup.py) | Existing connected versus completion/client-verification evidence and recovery actions | Surface evidence/freshness consistently; models-list reachability is weaker than an exact dedicated-route client completion. |
| [Harness profiles](../local_model_router/harnesses/profiles.py) | Existing validation, explicit configuration application and persistence | Review gaps before adding hook or publication protection; do not replace established safe helpers wholesale. |
| [Gateway/service](../local_model_router/service/app.py), [fleet manager](../local_model_router/service/fleet_manager.py) | Auth, streaming, pinned routes, explainable policy, bounded admission and operational SQLite metadata | A new research run needs private durable identities/events/artifacts; do not put content into fleet telemetry or use a second inference scheduler. |
| [Diagnostics](../local_model_router/diagnostics.py) | Allowlisted operational report | Inspectability and privacy checks can extend this report; external usage transmission is not selected. |
| [Chat V2 roadmap](1.0-beta-roadmap.html), [Spynel plan](spynel-adoption-plan.md) | Existing post-beta stream/queue/stop/rendering/history/provenance direction | Herdr adds design/test evidence, not a replacement Chat V2 project or a reason for React/GPUI. |
| [Future orchestration](future-orchestration.md) | Optional process boundary and HIVE task-ownership/recovery direction | No new general planner, dispatcher or terminal dependency is introduced by these docs. The deprecated orchestrator is not automatically a new Workbench backend. |

## Proposed component and data boundaries

- **Router:** inference access, authoritative harness pins, capacity/admission, disclosure, reason codes and content-free operational audit.
- **Existing browser:** stable chat state and additive contextual views; retain the current stack unless the separate frontend gate passes.
- **Optional Workbench (proposed):** private conversations, runs/attempts/events, selected sources, immutable artifact versions and reviewed action intents/receipts. A run is not a canonical commitment or a general scheduler task.
- **Canonical task service:** owns commitments and atomic expected-revision/idempotency/receipt behavior. The Workbench board is an authorized projection, not a second task database. No personal tasks are created by this repository TODO.
- **Optional execution adapter (later):** owns terminal/client processes and presentation. It does not own model policy or acquire authority by drawing a panel. Every worker uses a verified Imperium connection profile.

WB0 must specify storage roots via existing platform conventions, retention/export/clear scope, request/run/attempt identity, event sequencing/replay, cancellation and crash states before implementation. After restart, unfinished research is interrupted; reconnect attaches instead of resubmitting. Stable submission/action keys bind to normalized payload digests. Task effects in progress across a timeout/crash remain outcome-unknown until authoritative reconciliation.

Private content never enters generic telemetry, status previews, notifications, or logs. Selecting public-web research is an explicit source-network mode, separate from local/cloud model disclosure. Use model tiers `local`, `utility`, and explicitly approved `cloud`; preserve pins and deterministic privacy checks. Neither an available SSH target nor a helper's self-reported success grants permission.

## Source provenance and adoption modes

- Herdr TUI/runtime: [ee5bfb5041bb3e4a49d591c72308aae3c81ecdde](https://github.com/herdrdev/herdr/tree/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde), package 0.9.5.
- Herdr App: [1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf](https://github.com/herdrdev/herdr-app/tree/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf), package 0.1.13.
- The app depends on a separate herdr-core. Do not combine the TUI, app and core's protocol, persistence or resume guarantees. Source references below are pinned research evidence, not claims about newer upstream versions.
- **Port:** select and adapt actual small code/test units with provenance and applicable notices.
- **Reimplement:** use the narrow contract/algorithm in the existing Python/browser/PowerShell stack.
- **Inspire:** translate a useful interaction; do not assume the original UI supplies our storage, security or task semantics.

The initial 60-item inventory below is retained for traceability. Its Now/Workbench/Later labels are research classifications, not scheduling authority. The [work plan](herdr-work-plan.md) resolves package dependencies and existing gates; the [TODO](herdr-todo.md) owns implementation status. Each R ID has one primary package owner, so overlapping concepts do not create 60 mandatory projects.

## Source-backed development inventory


### Everyday product and review

| ID | Opportunity and user benefit | Verified source | Imperium application and boundary |
| --- | --- | --- | --- |
| R01 | Stable central conversation beside contextual work. Keep a question visible while inspecting its work. | App README spaces/panes and [peek controls][A-peek]. | Workbench / Inspire. Keep chat mounted; place selected run, source or artifact beside it. Terminal layout is inspiration, not a conversation/artifact implementation. |
| R02 | Preserve draft, focus and selected context during refresh/reconnect. | [App reconnect][A-window], `window.rs:625-726,805-822`. | Now / Reimplement. Current `dashboard/index.html:798-803` replaces the main DOM; composer has no draft binding. Use stable view state and incremental updates; also use existing gateway streaming for partial answers. |
| R03 | Peek then keep: inspect something briefly, promote only useful context to a panel/tab. | [App peek][A-peek], `main.js:19-78`. | Workbench / Inspire. Sources, results and worker details can open without navigation away. Rebuild browser focus, dismissal and keyboard behavior. |
| R04 | Attention list prioritizes blocked work, unseen results and working jobs. | [App agents][A-agents], `main.js:48-61,225-248,378-417`; `scripts/agents.test.ts`. | Workbench / Reimplement. One place for actions requiring the operator; show actual authoritative run states rather than inventing agents. |
| R05 | Separate result status from whether a person has seen it. | [App model][A-model], `model.rs:460-555`; [seen labels][A-labels]. | Workbench / Reimplement. Seen, agent-reported done, artifact saved and verified success remain separate. Acknowledging a result must not complete a canonical task. |
| R06 | Pin important work and group helper agents under their parent/project. | [App agents][A-agents], `main.js:225-248,378-417`. | Workbench / Inspire. Useful with multiple real jobs/connections; grouping is presentation, not ownership transfer. |
| R07 | Put quiet work away and resurface it when blocked. | [App model][A-model], `model.rs:409-435`; `window.rs:2082-2095`. | Workbench / Inspire. Reduce clutter without stopping, deleting or archiving the underlying task. |
| R08 | Searchable command palette showing real shortcuts and recent actions. | [App palette][A-palette], `main.js:80-129,150-184`; `scripts/palette.test.ts`. | Now / Port small matching logic or reimplement. `fuzzy()` is a realistic small candidate; rendering is tied to GPUI. Add Hebrew matching and browser accessibility checks. |
| R09 | One action registry feeds UI, shortcuts, CLI listing and documentation. | [App commands][A-commands], `commands.rs:1-112,156-167`. | Now / Reimplement a small registry for existing actions. Discoverability must not automatically authorize mutation. Avoid a general plugin framework. |
| R10 | Reject unknown action arguments before choosing a default target. | [App commands][A-commands], `validate_args():126-145`, `missing():147-153`; tests after `:176`. | Now / Reimplement using existing request models. A typo in a target field must fail rather than mutate the focused connection. Their validator is not a complete typed schema. |
| R11 | Bounded contextual file/artifact preview with explicit binary, too-large and error states. | [App files][A-files], `main.js:13-15,59-68,130-160`; `scripts/files.test.ts`. | Workbench / Port selected presentation/parsing logic. Restrict to the private artifact root; do not inherit arbitrary shell filesystem access. |
| R12 | Review changes using bounded diffs and precomputed line numbers. | [App changes][A-changes], `main.js:12-29,113-147`, `diffRows()`. | Workbench / Port small diff presentation helper. Useful for artifact revisions and task previews; Git commit controls are a separate authority. |
| R13 | Keep last usable settings when an edited configuration becomes invalid. | [App configuration reload][A-reload], `window.rs:867-940`. | Now / Reimplement only missing behavior around current validation. Preserve the invalid file and expose a clear reason; do not silently replace it with defaults. |
| R14 | Preserve local customizations when refreshing shipped defaults. | [App mods][A-mods], `sync_defaults():99-135`, `sync_layouts():138-181`; `keys.rs:174-188`. | Now / Reimplement around managed profile/template assets. Fingerprint known defaults; preserve edits and preview changes. Do not automatically overwrite user-owned client config. |
| R15 | Reject stale editor saves while retaining the user's draft. | [App mod files][A-mod-files], `write():26-75`; test after `:118`. | Workbench / Reimplement with authoritative revisions. The app's process-local mutex and rechecks are not cross-process atomic CAS. |

### Events, connection reliability and persistence

| ID | Opportunity and user benefit | Verified source | Imperium application and boundary |
| --- | --- | --- | --- |
| R16 | Independent monotonic event IDs even when retained history is trimmed. | [Herdr event hub][H-events], `push():21-31`. | Now / Reimplement. Direct fit for reproduced setup-event cursor defect described below. No Rust dependency or new event platform needed. |
| R17 | Distinguish empty history, lost history and unavailable history. | [Herdr event hub][H-events], `EventHistoryError:12-16`, `events_after_checked():46-66`; `subscriptions.rs:326-340`. | Now / Reimplement explicit gap and resync response. Do not report an unavailable store as an empty result. This event hub is in-memory, not durable. |
| R18 | Snapshot plus ordered changes with race-aware subscription startup. | [App event reducer][A-hc], `hc.rs:34-119`; Herdr `subscriptions.rs:397-539`. | Now / Reimplement. Ignore events covered by snapshot; resnapshot on missed events; test transition during subscription startup. Persisted Workbench replay is additional work. |
| R19 | Reject late replies from obsolete connection/worker attempts. | [Herdr supervisor][H-supervisor], `record_status():258-271`; App `window.rs:625-646,805-822`. | Now / Reimplement generation/attempt fence. A slower previous check must not overwrite a newer success or write into a replacement run. |
| R20 | Separate process/transport liveness from useful readiness. | [Herdr endpoint health][H-health], `health.rs:3-83`; [App sync][A-sync], `Slot::layout():24-47`. | Now / Reimplement targeted gaps. Imperium already has readiness and connected/verified distinctions; expose and test them rather than inventing another health system. |
| R21 | Revalidate freshness after sleep, wake and reconnection. | [Herdr endpoint health][H-health], `resume():39-42`; [supervisor][H-supervisor], `host_power_event():319-372`. | Now / Reimplement for browser visibility/resume and optional desktop shell. Display last-success/stale state, not indefinitely cached ready. |
| R22 | Independent reconnect state per target. | [Herdr supervisor][H-supervisor], `ReconnectState:51-125`, `reconcile_profiles():142-178`. | Now / Reimplement at client/connection boundary. One failed target must not reset healthy ones. Preserve existing inference admission/scheduling. |
| R23 | Classify repair-required errors and reset retry delay only after stable readiness. | [Herdr supervisor][H-supervisor], `:283-310,540-571`; [App sync][A-sync], `rpc_error():66-72`. | Now / Reimplement. Authentication/update/configuration needs an action; transient failures may retry. Herdr App's own reconnect loop uses a fixed delay; exponential policy comes from the TUI supervisor. |
| R24 | Bound queued output by entries and bytes, with explicit overflow behavior. | [Herdr writer][H-writer], `:12-16,125-150,186-193`. | Now or Workbench / Reimplement only if the event/stream path needs it. A slow dashboard must not grow memory without limit; ordered mutations cannot silently drop. |
| R25 | Keep slow writes off the UI loop and allow bounded cancellation/flush. | [Herdr writer][H-writer], `:29-85,218-251`; write/flush tests. | Workbench / Reimplement using current async stack. Terminal binary framing and Rust threads are not transferable implementation requirements. |
| R26 | Bound producer work per loop turn so shutdown and other clients stay responsive. | Herdr `src/server/headless.rs`, `EXTERNAL_EVENT_DRAIN_LIMIT`; `src/server/headless/tests/event_fairness.rs:24-105`. | Workbench / Port fairness scenarios when relevant. A bounded queue alone does not prove scheduling fairness. |
| R27 | Write, flush and atomically replace a snapshot before advertising it saved. | [Herdr persistence][H-persist], `save_json_to_path():49-66`; replacement-failure test `:238-281`. | Workbench / Reimplement through existing atomic helpers or SQLite transactions. This does not make multiple files one transaction or guarantee every caller propagates failure. |
| R28 | Preserve unread/unloaded/newer-version state before the first save. | [Herdr persistence writer][H-persist-writer], `preserve_unloaded():22-27`, `save():38-63`; `persist/io.rs:112-129`. | Now or Workbench / Reimplement. Opening a corrupt/newer record must not overwrite it with empty defaults. |
| R29 | Keep bounded recovery versions with monotonic ordering. | [Herdr persistence writer][H-persist-writer], `:82-159`. | Workbench / Inspire. Handle clock rollback; define retention explicitly. Do not copy recursive cleanup or treat best-effort logged writes as authoritative success receipts. |
| R30 | Separate private content/history from operational state and diagnostics. | [Herdr persistence][H-persist], `session_path():11-17`, `save_history_to_path():69-77`. | Workbench / Inspire. Dedicated private conversation/source/artifact store; fleet telemetry remains content-free. Merely splitting files is not encryption or access control. |
| R31 | Attach source, sequence and expiry to status reports. | Herdr `src/api/schema/panes.rs:403-423,447-476`, `PaneReportAgentParams`. | Workbench / Reimplement a narrow fixed status schema. Expire stale reports and reject older sequence; free-text agent metadata must not enter telemetry. |
| R32 | Version-bound document updates and explicit conflict reread. | [App sync][A-sync], `round_timeout():90-110`. | Workbench / Reimplement for settings/layout. Latest-state flushing is unsuitable for ordered task mutations; those require the canonical service's preconditions, idempotency and receipts. |
| R33 | One remaining deadline across write/conflict-read phases. | [App sync][A-sync], `round_timeout():90-110`, remaining-duration read. | Now / Reimplement in applicable connection/setup operations. A multi-phase operation should not restart its full timeout at every phase. |
| R34 | Disconnect is different from confirmed process exit or cancelled work. | [App stream][A-hc], `Stream::{Closed,Lost}:23-31`; `src/terminal.rs:411`. | Now and Workbench / Reimplement. Browser abort/connection loss does not prove the worker stopped. Preserve partial results and reconcile outcome. |
| R35 | Do not retry an ambiguous spawn/mutation result blindly. | App `src/window.rs:4639-4655`, `run_following_folder()`. | Workbench / Reimplement outcome-unknown/reconcile semantics. A timeout may occur after a process started; use a stable operation key before any safe retry. |

### Client integration and explicit contracts

| ID | Opportunity and user benefit | Verified source | Imperium application and boundary |
| --- | --- | --- | --- |
| R36 | Negotiate compatibility generation, minimum version and required capabilities. | [Herdr endpoint protocol][H-protocol], `:1-28,46-106,158-179`; [App client][A-hc], `check():178-235`. | Now / Reimplement as versioned client/profile metadata. Keep OpenAI-compatible HTTP; do not introduce their binary/RPC protocol. A handshake is not a successful model request. |
| R37 | Freeze supported released-client fixtures and test forward-compatible fields. | [Herdr endpoint protocol][H-protocol], tests `:262-295,337-371`; `tests/fixtures/endpoint-*-v1.json`. | Now / Port testing principle for promised request/response/stream contracts. Include installed and supported stable harness versions; schema tests do not replace live-client smoke. |
| R38 | Install hooks idempotently while preserving native client formats and unrelated hooks. | [Herdr config editing][H-config-edit], `ensure_command_hook():64-116`; native-format distinction `:118-123`. | Now / Reimplement around existing profile setup. Separate managed assets from user configuration. Exact-command dedupe is useful; automatic global normalization is not. |
| R39 | Check configuration file ownership/link/permission hazards around publication. | [Herdr config file][H-config-file], `:15-80`, `Replacement::prepare():88-130`, `commit():132-135`; child tests. | Now / Port missing checks. Preserve Imperium safeguards; platform semantics need Windows tests. The source explicitly does not provide multi-file transactions. |
| R40 | Decode Windows hook stdin as UTF-8 bytes independent of console code page. | [Herdr Codex hook][H-hook], `herdr-agent-state.ps1:13-22`. | Now / Port narrow reader when a hook is actually selected. Especially relevant to Hebrew payloads; changing console display alone is insufficient. |
| R41 | Bind reported client sessions to the right parent/thread and execution scope. | [Herdr Codex hook][H-hook], `:9-29`; Claude companion hook `:23-29`; `tests/cli/hooks.rs:128-231`. | Now / Reimplement minimal identity checks. Do not read transcripts merely because a path exists. A session report does not prove routing through Imperium. |
| R42 | Versioned declarative compatibility data for each supported client. | Herdr `distribution/agent-detection/index.toml`, `hermes.toml:1-6`; `src/detect/manifests/`. | Now / Inspire versioned setup/capability manifests. Keep terminal-detection regular expressions outside authoritative privacy/routing decisions. |
| R43 | Wait for actual interactive readiness after process launch. | Herdr `src/app/agents.rs:145-236`; `tests/cli/agents.rs` busy-shell, timeout and kind-mismatch cases. | Later / Port test scenarios to optional execution adapter. Process creation success is weaker than usable client; no new process-launch authority in the router. |
| R44 | Show source/confidence of inferred status and smooth transient idle. | Herdr `src/pane/agent_detection.rs:35-77,91-145`. | Later / Inspire terminal UX only. Current TUI uses terminal-screen heuristics; inferred blocked/idle/done never verifies task success. Prefer explicit reports where available. |
| R45 | Wait for a new state transition instead of immediately matching old done/idle. | Herdr `src/api/wait.rs:179-247,538-558`, `after_state_change_seq`. | Later / Reimplement bounded waits/cancellation for helper coordination. It prevents an old finished turn satisfying a new request; it is not a task verifier. |

### Windows lifecycle, evidence and optional execution

| ID | Opportunity and user benefit | Verified source | Imperium application and boundary |
| --- | --- | --- | --- |
| R46 | Record the first stop reason and initiating actor/request. | [Herdr shutdown][H-shutdown], `ShutdownReason`, `ServerStop::request():9-56`; tests `:64-87`. | Now / Reimplement small safe attribution. Useful for Studio/Hermes interruptions; safe actor IDs only, no prompts, secrets or raw command arguments. It explains stops; it does not solve shared GPU arbitration. |
| R47 | Stage installation beside current version, promote, restore on failure. | [App developer installer][A-install], `Install-HerdrAppFiles():2-37`. | Now / Port only missing behavior after comparing Imperium cleanroom recovery. Keep verified path boundaries and backup retention. This helper is not the public release installer. |
| R48 | Inject failure of promotion and then failure of rollback; verify backup survives. | [App installer tests][A-install-test], `:25-47`. | Now / Port high-value scenarios into existing Windows tests. A surviving backup and its actionable path matter even when rollback cannot complete. Tests were inspected, not run here. |
| R49 | Publish staged-update readiness only after checksum and executable-version verification. | [App updater][A-update], `check_from():272-310`, `staged_version():424-446`. | Now / Reimplement missing receipt semantics. Same-origin checksum is not independent manifest authentication; preserve actual installer trust requirements. |
| R50 | Recover interrupted updates without endless relaunch or unnecessary worker restart. | [App updater][A-update], `startup_target`, `apply_on_startup():439-470`; rollback `:481-499`. | Now / Inspire existing lifecycle. Separate UI update from model/process lifetime and active requests; validate the packaged path, not merely a source helper. |
| R51 | Preserve raw Windows PATH entries and registry value type. | [App developer installer][A-install], `:63-77`. | Now / Port targeted handling/tests if a gap exists. Do not expand literal environment variables and overwrite the user's original PATH representation. |
| R52 | Test local control ownership, stale endpoint cleanup and elevation boundaries. | Herdr `src/ipc.rs:99-155,331-350`; `tests/windows_server_security.rs:52+`. | Later / Port principles only if adding IPC. Existing loopback HTTP authentication remains authoritative; native SDDL code is not drop-in Python. |
| R53 | Track provenance and notice freshness for selected borrowed files and bundled binaries. | [App license inventory][A-inventory], [notice workflow][A-notice-doc]; `tests/notices.rs:92`; `scripts/test_distribution.py:230`. | Now if borrowing / Reimplement a smaller inventory gate. Track source SHA, file, destination, modifications and packaged notices; no Rust-wide dependency inventory needed. |
| R54 | Generate command/help examples from the implemented registry and detect drift. | App `scripts/docs.ts`, `scripts/docs.test.ts`; generated `docs/commands.md`. | Now / Inspire current API/CLI docs checks. Implement only where a shared registry already removes duplication; do not build another documentation system. |
| R55 | Repeatable isolated performance workloads with recorded settings and artifact output. | App `scripts/bench.py`, `scripts/test_bench.py`; README performance protocol. | Workbench / Port methodology for browser idle, long answer, many events and reconnect. Do not copy a runner that starts/stops arbitrary local processes. Render-call counters do not prove physical display FPS or inference speed. |
| R56 | Separate window lifetime from execution lifetime and show restore limits honestly. | Both READMEs; App [security model][A-security] storage/core separation. | Later / Inspire optional adapter. Closing UI can detach; closing a pane can terminate. App reboot restoration creates fresh shells, not old processes; TUI has separate supported session-resume logic. |
| R57 | Typed supported-session resume plans with argv validation and dedupe. | Herdr `src/agent_resume.rs:5-89,116-132,199-326`; resume tests. | Later / Reimplement optional adapter. Layout restored, session resumed and verified task recovery are different receipts. POSIX shell quoting restrictions are not Windows-safe execution. |
| R58 | Isolated Git worktree per coding activity, explicit dirty-state checks. | Herdr `src/worktree.rs:154-188,215-237`; `src/app/api/worktrees/deferred.rs:100-227`. | Later / Inspire existing worktree conventions. Never copy recursive forced-cleanup recovery at `worktree.rs:346-374`; canonical checkout and concurrent edits remain protected. |
| R59 | Make execution location explicit across local and SSH machines. | App README remote spaces; Herdr [endpoint supervisor][H-supervisor]. | Later / Inspire optional multi-machine surface. Independently reconnect each target; label actual location. SSH reachability is neither permission to disclose content nor proof model traffic uses Imperium. |
| R60 | Let the operator inspect exactly what diagnostics/telemetry contain. | App `src/telemetry.rs:217-269`, no-free-text test `:723`; `defaults/frame/settings/main.js:755-815`; [privacy docs][A-security]. | Now / Inspire existing safe diagnostics. Borrow report visibility, fixed fields and privacy tests. Do not inherit release telemetry-on default, installation-ID transmission or unrestricted mod network access. |


## Small source units worth evaluating


| Source unit | What to take | What to retain from Imperium |
| --- | --- | --- |
| `herdr/src/api/event_hub.rs`, `push`, `events_after_checked` | Monotonic cursor + explicit lost/unavailable result and boundary tests. | Python stack, content-free events; persisted research journal designed separately. |
| `herdr/src/client/endpoint/supervisor.rs`, `record_status` | Generation fence and stale-result test. | Existing health/admission owner; no second scheduler. |
| `herdr-app/src/hc.rs`, `EventLines` | Snapshot/reducer/resync contract and fragmented-input tests. | Current HTTP/stream contracts; stricter malformed-event validation. |
| `herdr-app/scripts/test-install-windows.ps1` | Promotion/rollback failure scenarios and backup-path assertions. | Existing installer, path checks and cleanroom harness; no upstream script execution. |
| `herdr/src/server/shutdown.rs`, `ServerStop` | First-cause attribution and tests. | Redacted actor/request metadata and current owned-process checks. |
| `herdr/src/integration/config_edit.rs`, `ensure_command_hook` | Exact-command dedupe preserving unrelated native hook groups. | Client-specific setup contracts, backups and explicit application authority. |
| `herdr/src/integration/assets/codex/herdr-agent-state.ps1` | Raw UTF-8 stdin reader, scoped identity checks, argv construction. | Minimal necessary data; no transcript ingestion; official current client-hook verification before implementation. |
| `herdr-app/defaults/frame/palette/main.js`, `fuzzy` | Small search/ranking logic and interaction scenarios. | Browser renderer, Hebrew strings, keyboard focus and accessibility. |
| `herdr-app/defaults/mods/changes/main.js`, `diffRows` | Bounded diff parsing/presentation helper if suitable. | Review-only scope and explicit action previews; no automatic Git commit actions. |
| `herdr-app/src/mod_files.rs`, `write` | Stale-edit detection and draft-preserving messages. | Stronger revision/transaction contract for cross-process or canonical task mutations. |

Rust files are primarily algorithm/test references for reimplementation. Small PowerShell/JavaScript units are more realistic direct adaptation candidates. No source was imported into Imperium in this session.


## Reuse exclusions and upgrade procedure

Do not import GPUI, Ghostty, QuickJS, terminal emulation or a plugin marketplace merely for a richer window. App mods have filesystem/process/network authority and are not a permission sandbox. Release telemetry-on defaults are not adopted. Inferred terminal status is not verified success, and an in-memory event buffer or restored pane layout is not durable task recovery.

The app's developer helper `scripts/install-windows.ps1` has fault-injected rollback tests; the distinct public `distribution/install.ps1` does not invoke that helper. Test the actual shipped path. Same-service checksums are not independent manifest signatures. Upstream keyboard/Hebrew/RTL/accessibility completeness was not verified.

Both root licenses declare Apache-2.0; selected assets/dependencies carry other terms. Before copying/adapting, record SHA/path/destination/modifications, retain relevant attribution, license and NOTICE content, and verify inclusion in each redistributed artifact. See the [official license](https://www.apache.org/licenses/LICENSE-2.0). Ideas implemented independently differ from copied/adapted expression; do not vendor unrelated fonts, branding or dependencies.

For an adopted unit: check upstream releases/diffs for the selected files and their dependencies/notices; inspect changed contracts/licenses; upgrade deliberately in an isolated branch; run the affected Windows/browser/client/privacy/recovery checks; update provenance. For an actually added dependency, check outdated packages, review official release notes, upgrade the locked dependency and rerun those checks. No automatic wholesale vendor synchronization or new mandatory Ollama/Docker/agent framework.

## Decision and verification boundaries

No new Accepted ADR is created by this planning change. Before implementing new persistence/retention, task mutation, execution authority, public contracts or frontend runtime changes, draft the narrow ADR(s), reconcile existing owners and obtain the designated product decision. Small existing-path fixes do not require a new architecture project merely because they share this inventory.

Evidence inspected: pinned source/license/test files and local source baseline. Upstream tests were not run; native UI, installer, updates, hooks, models and client routing were not exercised. The prior method-level setup-event reproduction is distinct from a live setup smoke. Implementation must refresh exact affected source/client versions and record new checks.

Docs-only validation is `git diff --check`, local links/IDs/dependency consistency, and preservation of the active checkout. No runtime code, version, user-visible behavior, configuration or installation changes here; changelog/release fragment and runtime suite are not applicable to this planning-only diff. Future code slices follow [RELEASE](../RELEASE.md) and the owning DOX tests.

[H-events]: https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/api/event_hub.rs#L18
[H-supervisor]: https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/client/endpoint/supervisor.rs#L258
[H-health]: https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/client/endpoint/health.rs#L3
[H-writer]: https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/client/endpoint/writer.rs#L12
[H-persist]: https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/persist/io.rs#L49
[H-persist-writer]: https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/persist/writer.rs#L22
[H-protocol]: https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/protocol/endpoint.rs#L262
[H-config-edit]: https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/integration/config_edit.rs#L64
[H-config-file]: https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/integration/config_file.rs#L15
[H-hook]: https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/integration/assets/codex/herdr-agent-state.ps1#L13
[H-shutdown]: https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/server/shutdown.rs#L9
[A-peek]: https://github.com/herdrdev/herdr-app/blob/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf/defaults/mods/peek/main.js#L19
[A-window]: https://github.com/herdrdev/herdr-app/blob/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf/src/window.rs#L625
[A-reload]: https://github.com/herdrdev/herdr-app/blob/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf/src/window.rs#L867
[A-agents]: https://github.com/herdrdev/herdr-app/blob/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf/defaults/mods/agents/main.js#L225
[A-model]: https://github.com/herdrdev/herdr-app/blob/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf/src/model.rs#L409
[A-labels]: https://github.com/herdrdev/herdr-app/blob/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf/docs/labels.md#L15
[A-palette]: https://github.com/herdrdev/herdr-app/blob/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf/defaults/frame/palette/main.js#L80
[A-commands]: https://github.com/herdrdev/herdr-app/blob/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf/src/commands.rs#L124
[A-files]: https://github.com/herdrdev/herdr-app/blob/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf/defaults/mods/files/main.js#L13
[A-changes]: https://github.com/herdrdev/herdr-app/blob/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf/defaults/mods/changes/main.js#L12
[A-mods]: https://github.com/herdrdev/herdr-app/blob/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf/src/mods.rs#L99
[A-mod-files]: https://github.com/herdrdev/herdr-app/blob/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf/src/mod_files.rs#L26
[A-hc]: https://github.com/herdrdev/herdr-app/blob/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf/src/hc.rs#L34
[A-sync]: https://github.com/herdrdev/herdr-app/blob/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf/src/mirror_sync.rs#L24
[A-install]: https://github.com/herdrdev/herdr-app/blob/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf/scripts/install-windows.ps1#L2
[A-install-test]: https://github.com/herdrdev/herdr-app/blob/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf/scripts/test-install-windows.ps1#L25
[A-update]: https://github.com/herdrdev/herdr-app/blob/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf/src/update.rs#L272
[A-inventory]: https://github.com/herdrdev/herdr-app/blob/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf/licenses/inventory.json
[A-notice-doc]: https://github.com/herdrdev/herdr-app/blob/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf/licenses/README.md
[A-security]: https://github.com/herdrdev/herdr-app/blob/1b8e79b39118c0fc0e4e92c6fba0afd592a5b4bf/docs/security.md

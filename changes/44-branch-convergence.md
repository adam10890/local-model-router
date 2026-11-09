# One active branch, and a check that enforces it

The one-branch rule was already written in four places — `CLAUDE.md`, the root
`AGENTS.md`, `docs/development/AGENTS.md` and `docs/development/git-workflow.md`
— and `origin` still carried ten stale branches, nine of them already merged
and one squash-merged in PR #19 that git still reported as open. Prose was not
the enforcement mechanism.

**`scripts/branch_guard.py`** (stdlib only) counts `dev/*`, `ready/*` and the
agent prefixes `claude/*`, `codex/*` and `cursor/*` on a remote, exempts
`spike/*`, and exits non-zero past the allowance. The agent prefixes are
counted because an agent branch is the kind that accumulates unnoticed: PR #24
arrived from `cursor/llama-cpp-b12110` while this was being written, and the
first version of the guard did not count it. For each branch it says whether the branch still holds work of its
own. The test is not how many files match but, for each file that still
differs, whether `main` changed that file too since the merge base: a file that
differs because `main` moved on is work `main` already has, while a file only
the branch holds is work to merge. A share-of-files threshold gets this wrong —
`ready/hermes-gpu-swap` matched 41 of 51 files and was still fully merged, and
merging it would have reverted the version and the beta evidence table.
`--allow N` covers the owned-parallel case; `--queue` requires each open
branch's slug to appear in the idea queue.

Deliberately not a CI job: a check on `main` that counts open branches would
fail pull requests for reasons unrelated to their diff.

**The rule now has one home.** `docs/development/git-workflow.md` gains
"Starting a task: the one-branch gate" as the authoritative statement; the other
three copies become pointers to it. The clause they were all missing is the one
that does the work: *while a branch is open, no new development starts, and a
new idea goes into the queue rather than into a branch.*

**`docs/future-improvements.md` is now the single FIFO idea queue.** Five
documents previously held candidate work and none declared itself the queue, so
two of them carried competing priority schemes. Entries are `Q<n>`, assigned on
arrival and never renumbered; position is arrival order; an idea that advances,
enables or specialises an entry already queued is folded into that entry's
*Folds in* list and inherits its position rather than being queued behind it; an
entry leaves only by shipping or by explicit rejection, each recorded. The other
planning documents keep their content and own *detail* — `docs/AGENTS.md` now
says the queue owns *order* and they do not.

Three contradictions between accepted documents are recorded as open questions
rather than settled here: who owns task policy (`future-orchestration.md`
against `spynel-adoption-plan.md`), which system provides the canonical task
service that three Herdr documents require, and whether a plugin framework is
accepted or refused. Each is the founder's call.

Also: `docs/spynel-adoption-plan.md` gains the ownership line it never had, and
`docs/1.0-beta-roadmap.html` no longer reads "Imperium 0.9.0" from 2026-08-05.

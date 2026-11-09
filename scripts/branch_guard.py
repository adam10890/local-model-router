#!/usr/bin/env python3
"""Fail when more than one task branch is open.

The one-branch rule is written in four places and was still broken three times
in one session, so prose is evidently not the enforcement mechanism. This is.

Run it before opening a branch:

    python scripts/branch_guard.py

It counts `dev/*`, `ready/*` and `claude/*` on the configured remote. `spike/*`
is exempt by design -- a throwaway experiment is not a task branch -- and so is
`main`.

For each branch it also says whether the branch still holds work of its own,
because git calls a squash-merged branch "open" and merging one again
resurrects the state it was merged from. That is the trap `ready/hermes-gpu-swap`
sat in: 41 of its 51 files were byte-identical to `main`, the other ten had
moved on *on main*, and merging it would have reverted the version and the beta
evidence table.

Exit codes: 0 within the allowance, 1 over it, 2 when git itself failed.

Stdlib only. The one network call is the `git ls-remote` the count requires.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Optional

TASK_PREFIXES = ("dev/", "ready/", "claude/")
EXEMPT_PREFIXES = ("spike/",)


class GitError(RuntimeError):
    pass


def _git(*args: str) -> str:
    try:
        done = subprocess.run(("git",) + args, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise GitError(f"could not run git: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip()
        raise GitError(detail or f"git {' '.join(args)} failed")
    return done.stdout


def task_branches(remote: str) -> list[str]:
    """Open task branches on `remote`, in a stable order."""
    names: list[str] = []
    prefix = "refs/heads/"
    for line in _git("ls-remote", "--heads", remote).splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].startswith(prefix):
            continue
        name = parts[1][len(prefix):]
        if name == "main" or name.startswith(EXEMPT_PREFIXES):
            continue
        if name.startswith(TASK_PREFIXES):
            names.append(name)
    return sorted(names)


def _ahead_behind(main_ref: str, branch_ref: str) -> Optional[tuple[int, int]]:
    try:
        out = _git("rev-list", "--left-right", "--count", f"{main_ref}...{branch_ref}")
    except GitError:
        return None
    try:
        behind, ahead = (int(part) for part in out.split())
    except ValueError:
        return None
    return ahead, behind


def _blob(ref: str, path: str) -> Optional[str]:
    """The blob id of `path` at `ref`, or None when the file is absent there."""
    try:
        return _git("rev-parse", f"{ref}:{path}").strip() or None
    except GitError:
        return None


def classify(main_ref: str, branch_ref: str) -> Optional[tuple[str, str]]:
    """Classify a branch by whether its own work exists only on it.

    Returns (verdict, detail), or None when the local clone cannot answer.

    The useful question is not "how many files match" but, for each file that
    still differs, "did main change that file too since the merge base?" A file
    that differs because main moved on is work main already has under a later
    revision. A file that differs and main never touched is work only this
    branch holds. That distinction separates a squash-merged leftover from a
    branch with something to merge; a share-of-files threshold gets it wrong,
    since `ready/hermes-gpu-swap` matched 41 of 51 files and was still merged.
    """
    try:
        base = _git("merge-base", main_ref, branch_ref).strip()
        changed = _git("diff", "--name-only", base, branch_ref).strip()
        moved_on_main = _git("diff", "--name-only", base, main_ref).strip()
    except GitError:
        return None

    if not changed:
        return "empty", "changes nothing against main -- delete it"

    paths = changed.splitlines()
    main_touched = set(moved_on_main.splitlines())
    differing = [p for p in paths if _blob(main_ref, p) != _blob(branch_ref, p)]
    matching = len(paths) - len(differing)

    if not differing:
        return "integrated", (
            f"ALREADY ON MAIN -- all {matching} file(s) it changed are identical. "
            "Delete it; never re-merge."
        )

    only_here = [p for p in differing if p not in main_touched]
    if not only_here:
        shown = ", ".join(differing[:4]) + (" ..." if len(differing) > 4 else "")
        return "superseded", (
            f"ALREADY ON MAIN under a later revision -- {matching}/{len(paths)} files "
            f"identical, and main itself changed the other {len(differing)}.\n"
            f"      Differing: {shown}\n"
            "      Merging would revert main to the state it was merged from. Delete it."
        )

    shown = ", ".join(only_here[:4]) + (" ..." if len(only_here) > 4 else "")
    return "work", (
        f"carries work -- {len(only_here)} of {len(paths)} file(s) exist only here: "
        f"{shown}\n      Finish, verify, merge, then delete."
    )


def describe(main_ref: str, branch: str, branch_ref: str) -> str:
    counts = _ahead_behind(main_ref, branch_ref)
    where = (
        "ahead/behind unknown -- run `git fetch`"
        if counts is None
        else f"ahead {counts[0]}, behind {counts[1]}"
    )
    state = classify(main_ref, branch_ref)
    detail = "state unknown -- fetch the remote and check by hand" if state is None else state[1]
    return f"  {branch}  ({where})\n      {detail}"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when more than one task branch is open.",
        epilog="The rule it enforces lives in docs/development/git-workflow.md.",
    )
    parser.add_argument("--remote", default="origin", help="remote to inspect (default: origin)")
    parser.add_argument(
        "--allow",
        type=int,
        default=1,
        help="how many task branches may be open (default 1). A parallel branch needs "
        "an explicit owner and an inventory entry; see the git-workflow doc.",
    )
    parser.add_argument(
        "--queue",
        metavar="PATH",
        nargs="?",
        const="docs/future-improvements.md",
        help="also require each open branch's slug to appear in the idea queue",
    )
    args = parser.parse_args(argv)

    if args.allow < 0:
        parser.error("--allow cannot be negative")

    try:
        branches = task_branches(args.remote)
    except GitError as exc:
        print(f"branch_guard: {exc}", file=sys.stderr)
        return 2

    main_ref = f"{args.remote}/main"
    if not branches:
        print(f"branch_guard: no task branch open on {args.remote}. Clear to start one.")
        return 0

    print(f"branch_guard: {len(branches)} task branch(es) open on {args.remote}:")
    for branch in branches:
        print(describe(main_ref, branch, f"{args.remote}/{branch}"))

    failed = False
    if len(branches) > args.allow:
        print(
            f"\nbranch_guard: FAIL -- {len(branches)} open, {args.allow} allowed.\n"
            "Finish or delete the extras before starting new work. A new idea goes to\n"
            "the queue, not to a new branch.",
            file=sys.stderr,
        )
        failed = True

    if args.queue:
        try:
            with open(args.queue, encoding="utf-8") as handle:
                queue_text = handle.read()
        except OSError as exc:
            print(f"\nbranch_guard: cannot read the queue at {args.queue}: {exc}", file=sys.stderr)
            failed = True
        else:
            for branch in branches:
                slug = branch.split("/", 1)[1] if "/" in branch else branch
                if slug not in queue_text:
                    print(
                        f"\nbranch_guard: FAIL -- {branch} matches no entry in {args.queue}.\n"
                        "A branch should implement a queued idea, not arrive beside it.",
                        file=sys.stderr,
                    )
                    failed = True

    if failed:
        return 1
    print("\nbranch_guard: OK -- within the allowance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

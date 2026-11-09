"""Tests for scripts/branch_guard.py.

The guard exists because the one-branch rule was written in four places and
broken anyway, so these tests check the failing direction as carefully as the
passing one: a guard that only ever exits 0 enforces nothing.

Hermetic: each test builds a throwaway bare repo as `origin` and a clone beside
it. No network, no fixtures from the real repository.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "branch_guard", REPO_ROOT / "scripts" / "branch_guard.py"
)
assert _SPEC and _SPEC.loader
branch_guard = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(branch_guard)


def _run(cwd, *args):
    done = subprocess.run(
        ("git",) + args, cwd=cwd, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {done.stderr or done.stdout}")
    return done.stdout


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A clone with `origin` pointing at a bare repo, chdir'd into.

    Returns a helper exposing the clone path plus `write`, `commit` and
    `push_branch`, so each test states only the history it cares about.
    """
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _run(work, "config", "user.email", "test@example.invalid")
    _run(work, "config", "user.name", "test")

    class Helper:
        path = work

        def write(self, name, text):
            (work / name).write_text(text, encoding="utf-8")
            _run(work, "add", name)

        def commit(self, message):
            _run(work, "commit", "-q", "-m", message)

        def push(self, ref="main"):
            _run(work, "push", "-q", "origin", ref)

        def checkout(self, ref, new=False):
            _run(work, "checkout", "-q", *(["-b"] if new else []), ref)

    helper = Helper()
    helper.write("seed.txt", "seed\n")
    helper.commit("seed")
    helper.push()
    monkeypatch.chdir(work)
    return helper


def _classify(branch):
    return branch_guard.classify("origin/main", f"origin/{branch}")


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------

class TestCounting:
    def test_no_task_branch_passes(self, repo, capsys):
        assert branch_guard.main([]) == 0
        assert "no task branch open" in capsys.readouterr().out

    def test_one_branch_is_within_the_allowance(self, repo):
        repo.checkout("dev/one", new=True)
        repo.write("a.txt", "a\n")
        repo.commit("a")
        repo.push("dev/one")
        assert branch_guard.main([]) == 0

    def test_two_branches_fail(self, repo, capsys):
        for name in ("dev/one", "dev/two"):
            repo.checkout("main")
            repo.checkout(name, new=True)
            repo.write(f"{name[-3:]}.txt", "x\n")
            repo.commit(name)
            repo.push(name)
        assert branch_guard.main([]) == 1
        assert "FAIL" in capsys.readouterr().err

    def test_allow_raises_the_ceiling(self, repo):
        for name in ("dev/one", "dev/two"):
            repo.checkout("main")
            repo.checkout(name, new=True)
            repo.write(f"{name[-3:]}.txt", "x\n")
            repo.commit(name)
            repo.push(name)
        assert branch_guard.main(["--allow", "2"]) == 0

    def test_a_spike_is_not_a_task_branch(self, repo):
        for name in ("dev/one", "spike/idea"):
            repo.checkout("main")
            repo.checkout(name, new=True)
            repo.write(f"{name.split('/')[1]}.txt", "x\n")
            repo.commit(name)
            repo.push(name)
        assert branch_guard.task_branches("origin") == ["dev/one"]
        assert branch_guard.main([]) == 0

    def test_main_is_never_counted(self, repo):
        assert "main" not in branch_guard.task_branches("origin")

    @pytest.mark.parametrize("prefix", ["dev", "ready", "claude", "codex", "cursor"])
    def test_every_working_prefix_is_counted(self, repo, prefix):
        """An uncounted prefix is an unbounded number of branches.

        PR #24 arrived from `cursor/llama-cpp-b12110` while this guard was
        being written, and the first version of the guard did not count it.
        """
        name = f"{prefix}/thing"
        repo.checkout(name, new=True)
        repo.write("x.txt", "x\n")
        repo.commit("work")
        repo.push(name)
        assert branch_guard.task_branches("origin") == [name]

    def test_a_negative_allowance_is_rejected(self, repo):
        with pytest.raises(SystemExit):
            branch_guard.main(["--allow", "-1"])


# ---------------------------------------------------------------------------
# Classification -- the part that stops a squash-merged branch being re-merged
# ---------------------------------------------------------------------------

class TestClassification:
    def test_a_branch_with_its_own_file_carries_work(self, repo):
        repo.checkout("dev/live", new=True)
        repo.write("only-here.txt", "mine\n")
        repo.commit("live work")
        repo.push("dev/live")
        verdict, detail = _classify("dev/live")
        assert verdict == "work"
        assert "only-here.txt" in detail

    def test_a_branch_at_main_changes_nothing(self, repo):
        repo.checkout("dev/stale", new=True)
        repo.push("dev/stale")
        assert _classify("dev/stale")[0] == "empty"

    def test_a_normally_merged_branch_changes_nothing(self, repo):
        """After a real merge the branch tip *is* the merge base.

        So the branch's own diff is empty. That is a different code path from
        the squash case below, and the same actionable verdict: delete it.
        """
        repo.checkout("dev/done", new=True)
        repo.write("shared.txt", "v2\n")
        repo.commit("change shared")
        repo.push("dev/done")
        repo.checkout("main")
        _run(repo.path, "merge", "-q", "--no-edit", "dev/done")
        repo.push()
        verdict, detail = _classify("dev/done")
        assert verdict == "empty"
        assert "delete it" in detail

    def test_a_squash_merged_branch_main_has_not_moved_is_already_on_main(self, repo):
        """Squash-merged, and main has not touched those files since.

        Every file the branch changed is byte-identical on main, so there is
        nothing to merge and nothing ambiguous about it.
        """
        repo.checkout("dev/squashed-clean", new=True)
        repo.write("shared.txt", "from-branch\n")
        repo.commit("branch work")
        repo.push("dev/squashed-clean")
        repo.checkout("main")
        repo.write("shared.txt", "from-branch\n")
        repo.write("unrelated.txt", "main moved elsewhere\n")
        repo.commit("squash of dev/squashed-clean, plus unrelated work")
        repo.push()
        verdict, detail = _classify("dev/squashed-clean")
        assert verdict == "integrated"
        assert "ALREADY ON MAIN" in detail
        assert "never re-merge" in detail

    def test_a_squash_merged_branch_is_reported_as_superseded(self, repo):
        """The `ready/hermes-gpu-swap` case, reproduced.

        The branch's content reached main by squash, so git still calls the
        branch unmerged. Main then moved the same file on. Merging the branch
        now would revert main to the state it was merged from -- which is why
        the verdict has to be *delete*, not *merge*, and why a
        share-of-files threshold is the wrong test.
        """
        repo.checkout("dev/squashed", new=True)
        repo.write("shared.txt", "from-branch\n")
        repo.write("also.txt", "same-on-both\n")
        repo.commit("branch work")
        repo.push("dev/squashed")

        repo.checkout("main")
        # The squash: main takes the same content under a different commit.
        repo.write("shared.txt", "from-branch\n")
        repo.write("also.txt", "same-on-both\n")
        repo.commit("squash of dev/squashed")
        # And then main moves that file on, as it does while a branch lingers.
        repo.write("shared.txt", "moved-on\n")
        repo.commit("later change on main")
        repo.push()

        verdict, detail = _classify("dev/squashed")
        assert verdict == "superseded"
        assert "would revert main" in detail
        assert "Delete it" in detail

    def test_a_branch_whose_file_main_never_touched_still_carries_work(self, repo):
        """The guard must not call a live branch merged just because main moved.

        Main changing *other* files is the normal case; only a file the branch
        alone holds proves there is something to merge.
        """
        repo.checkout("dev/live", new=True)
        repo.write("untouched-by-main.txt", "mine\n")
        repo.commit("branch work")
        repo.push("dev/live")
        repo.checkout("main")
        repo.write("elsewhere.txt", "main moved\n")
        repo.commit("unrelated main work")
        repo.push()
        assert _classify("dev/live")[0] == "work"

    def test_an_unknown_ref_does_not_raise(self, repo):
        assert branch_guard.classify("origin/main", "origin/does-not-exist") is None


# ---------------------------------------------------------------------------
# The queue check
# ---------------------------------------------------------------------------

class TestQueueCheck:
    def _one_branch(self, repo, name="dev/observability"):
        repo.checkout(name, new=True)
        repo.write("x.txt", "x\n")
        repo.commit("work")
        repo.push(name)

    def test_a_slug_in_the_queue_passes(self, repo, tmp_path):
        self._one_branch(repo)
        queue = tmp_path / "queue.md"
        queue.write_text("### Q1 — Observability\n- Slug: `observability`\n", encoding="utf-8")
        assert branch_guard.main(["--queue", str(queue)]) == 0

    def test_a_slug_missing_from_the_queue_fails(self, repo, tmp_path, capsys):
        self._one_branch(repo, "dev/not-an-idea")
        queue = tmp_path / "queue.md"
        queue.write_text("### Q1 — Observability\n- Slug: `observability`\n", encoding="utf-8")
        assert branch_guard.main(["--queue", str(queue)]) == 1
        assert "matches no entry" in capsys.readouterr().err

    def test_an_unreadable_queue_fails_rather_than_passing_quietly(self, repo, tmp_path, capsys):
        self._one_branch(repo)
        assert branch_guard.main(["--queue", str(tmp_path / "missing.md")]) == 1
        assert "cannot read the queue" in capsys.readouterr().err

    def test_the_real_queue_covers_the_real_branch_slugs(self):
        """The repository's own queue must name every slug it plans to use."""
        queue = (REPO_ROOT / "docs" / "future-improvements.md").read_text(encoding="utf-8")
        for slug in ("observability", "cache-aware-routing", "herdr", "branch-convergence"):
            assert slug in queue


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

def test_a_broken_remote_exits_two(repo, capsys):
    assert branch_guard.main(["--remote", "no-such-remote"]) == 2
    assert "branch_guard:" in capsys.readouterr().err


def test_git_missing_is_reported_not_crashed(monkeypatch):
    def boom(*_a, **_kw):
        raise OSError("no git here")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(branch_guard.GitError):
        branch_guard._git("status")

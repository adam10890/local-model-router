"""Tests for tools/adr.py index generation and linting."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import adr  # noqa: E402


def test_adr_index_strips_blockquote_lines_from_status_cell(tmp_path):
    adr_dir = tmp_path / "adr"
    adr_dir.mkdir()
    (adr_dir / "0001-example-decision.md").write_text(
        "# 0001. Example decision\n\n"
        "Date: 2026-11-23\n\n"
        "## Status\n\n"
        "Accepted 2026-11-23 (v1.0)\n"
        "> Summary blockquote that must stay out of README status column.\n\n"
        "## Context\n\nSome context.\n\n"
        "## Decision\n\nWe will do X.\n\n"
        "## Consequences\n\nNegative: trade-off Y.\n",
        encoding="utf-8",
    )

    rendered = adr.index_text(adr_dir)
    assert "| 0001 | [Example decision](0001-example-decision.md) | Accepted 2026-11-23 (v1.0) | 2026-11-23 |" in rendered
    assert "Summary blockquote" not in rendered


def test_repo_adr_lint_passes():
    done = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "adr.py"), "lint"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "0 errors, 0 warnings" in done.stdout

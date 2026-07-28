from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HERMES = ROOT / "integrations" / "hermes" / "imperium-work-pages"
PI = ROOT / "integrations" / "pi" / "imperium-work-pages"


def test_hermes_skill_and_client_are_runnable():
    skill = (HERMES / "SKILL.md").read_text(encoding="utf-8")
    script = HERMES / "scripts" / "work_pages.py"

    assert "name: imperium-work-pages" in skill
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert "Hermes client for Imperium work pages" in result.stdout


def test_pi_package_tools_match_router_contract():
    package = json.loads((PI / "package.json").read_text(encoding="utf-8"))
    source = (PI / "extensions" / "work-pages.ts").read_text(encoding="utf-8")

    assert package["pi"]["extensions"] == ["./extensions"]
    assert "dependencies" not in package
    for tool in (
        "imperium_step_read",
        "imperium_step_claim",
        "imperium_step_log",
        "imperium_step_complete",
        "imperium_step_block",
    ):
        assert f'name: "{tool}"' in source
    for action in ("claim", "log", "complete", "block"):
        assert f"/{action}`" in source

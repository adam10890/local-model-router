"""`local-model-router disclosure` — templates, classification, and checks.

The CLI is the operator's leak check, so its own output is asserted to quote
nothing it found.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_model_router import cli  # noqa: E402

_SECRET = "sk-abcdefghijklmnopqrstuvwx"

_CLEAN_BRIEF = (
    "content_class: generic_scaffold\n\n"
    "## requirements\nParse a newline-delimited record stream.\n\n"
    "## interface\nparse(stream) -> rows\n\n"
    "## acceptance\nMalformed rows raise ParseError with the line number.\n\n"
    "## added_locally\nReal field names and the calling module.\n"
)


@pytest.fixture()
def conf(tmp_path, monkeypatch):
    """Point the CLI at an isolated conf dir with no disclosure override."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    (conf_dir / "llama_cpp_servers.yaml").write_text(
        "active_slots: []\nglobal:\n  backend: remote\n", encoding="utf-8"
    )
    (conf_dir / "upstreams.yaml").write_text(
        "upstreams:\n"
        "  - name: trusted\n"
        "    type: openai_compatible\n"
        "    base_url: http://x/v1\n"
        "    enabled: true\n"
        "    trust_tier: local_uncensored\n"
        "  - name: public\n"
        "    type: openai_compatible\n"
        "    base_url: http://y/v1\n"
        "    enabled: true\n"
        "    trust_tier: other_provider\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("A0_LMM_ROUTER_CONFIG", str(conf_dir / "llama_cpp_servers.yaml"))
    return conf_dir


def _write(tmp_path, name, text, *, encoding="utf-8"):
    path = tmp_path / name
    path.write_text(text, encoding=encoding)
    return str(path)


class TestListing:
    def test_bare_command_prints_the_ladder(self, conf, capsys):
        assert cli.main(["disclosure"]) == 0
        out = capsys.readouterr().out
        assert "local_uncensored" in out
        assert "other_provider" in out
        assert "undeclared executors resolve to: other_provider" in out

    def test_list_json_is_structured(self, conf, capsys):
        assert cli.main(["disclosure", "--list", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert [t["id"] for t in payload["trust_tiers"]][0] == "local_uncensored"
        assert payload["default_executor_tier"] == "other_provider"


class TestTemplate:
    def test_template_prints_required_sections(self, conf, capsys):
        assert cli.main(["disclosure", "--template", "generic_scaffold"]) == 0
        out = capsys.readouterr().out
        assert "content_class: generic_scaffold" in out
        for section in ("requirements", "interface", "acceptance", "added_locally"):
            assert f"## {section}" in out

    def test_unknown_class_fails(self, conf, capsys):
        assert cli.main(["disclosure", "--template", "nope"]) == 1
        assert "FAIL" in capsys.readouterr().out

    def test_template_output_passes_its_own_check(self, conf, tmp_path, capsys):
        cli.main(["disclosure", "--template", "product_feature"])
        rendered = capsys.readouterr().out
        path = _write(tmp_path, "t.md", rendered)
        # An empty template is missing nothing structural and leaks nothing.
        assert cli.main(["disclosure", "--check", path]) == 0


class TestClassify:
    def test_classify_reports_class_and_cap(self, conf, tmp_path, capsys):
        path = _write(tmp_path, "b.md", _CLEAN_BRIEF)
        assert cli.main(["disclosure", "--classify", path]) == 0
        out = capsys.readouterr().out
        assert "generic_scaffold" in out
        assert "other_provider" in out

    def test_classify_json(self, conf, tmp_path, capsys):
        path = _write(tmp_path, "b.md", _CLEAN_BRIEF)
        assert cli.main(["disclosure", "--classify", path, "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["content_class"] == "generic_scaffold"
        assert payload["declared"] is True

    def test_missing_file_fails_cleanly(self, conf, tmp_path, capsys):
        assert cli.main(["disclosure", "--classify", str(tmp_path / "nope.md")]) == 1
        assert "FAIL" in capsys.readouterr().out


class TestCheck:
    def test_clean_brief_passes(self, conf, tmp_path, capsys):
        path = _write(tmp_path, "b.md", _CLEAN_BRIEF)
        assert cli.main(["disclosure", "--check", path]) == 0
        assert "OK: brief satisfies the disclosure policy" in capsys.readouterr().out

    def test_missing_section_fails(self, conf, tmp_path, capsys):
        path = _write(tmp_path, "b.md", "content_class: generic_scaffold\n\n## requirements\nDo it.\n")
        assert cli.main(["disclosure", "--check", path]) == 1
        assert "MISSING" in capsys.readouterr().out

    def test_forbidden_content_fails(self, conf, tmp_path, capsys):
        path = _write(tmp_path, "b.md", _CLEAN_BRIEF + f"\napi_key: {_SECRET}\n")
        assert cli.main(["disclosure", "--check", path]) == 1
        assert "FORBIDDEN" in capsys.readouterr().out

    def test_check_output_never_echoes_what_it_found(self, conf, tmp_path, capsys):
        leaky = (
            _CLEAN_BRIEF
            + f"\napi_key: {_SECRET}\n"
            + "Load from C:\\Users\\someone\\models so that we can beat them.\n"
            + "Model file is Some-Model-Q8_0.gguf on the RTX 4090.\n"
        )
        path = _write(tmp_path, "b.md", leaky)
        assert cli.main(["disclosure", "--check", path, "--json"]) == 1
        out = capsys.readouterr().out
        for fragment in (
            _SECRET,
            "C:\\Users\\someone",
            "Some-Model-Q8_0.gguf",
            "RTX 4090",
            "beat them",
        ):
            assert fragment not in out
        payload = json.loads(out)
        assert "assigned_secret" in {f["pattern_id"] for f in payload["brief"]["scan"]["findings"]}

    def test_utf8_bom_is_tolerated(self, conf, tmp_path):
        # Windows editors commonly leave a BOM; it must not break parsing.
        path = _write(tmp_path, "b.md", _CLEAN_BRIEF, encoding="utf-8-sig")
        assert cli.main(["disclosure", "--check", path]) == 0


class TestCheckAgainstTarget:
    def test_permitted_target_passes(self, conf, tmp_path, capsys):
        path = _write(tmp_path, "b.md", _CLEAN_BRIEF)
        assert cli.main(["disclosure", "--check", path, "--target", "public"]) == 0
        assert "decision      : allow" in capsys.readouterr().out

    def test_target_below_the_cap_is_denied(self, conf, tmp_path, capsys):
        brief = _CLEAN_BRIEF.replace("generic_scaffold", "operator_data")
        path = _write(tmp_path, "b.md", brief)
        assert cli.main(["disclosure", "--check", path, "--target", "public"]) == 1
        out = capsys.readouterr().out
        assert "decision      : deny" in out
        assert "requires_tier:local_uncensored" in out

    def test_trusted_target_accepts_the_same_brief(self, conf, tmp_path, capsys):
        brief = _CLEAN_BRIEF.replace("generic_scaffold", "operator_data")
        path = _write(tmp_path, "b.md", brief)
        assert cli.main(["disclosure", "--check", path, "--target", "trusted"]) == 0
        assert "decision      : allow" in capsys.readouterr().out

    def test_unknown_target_is_treated_as_untrusted(self, conf, tmp_path, capsys):
        brief = _CLEAN_BRIEF.replace("generic_scaffold", "product_feature")
        path = _write(tmp_path, "b.md", brief)
        assert cli.main(["disclosure", "--check", path, "--target", "never-configured"]) == 1
        assert "undeclared" in capsys.readouterr().out


class TestOverrideRules:
    def test_conf_override_is_picked_up(self, conf, tmp_path, capsys):
        import yaml

        packaged = yaml.safe_load(
            (REPO_ROOT / "local_model_router" / "disclosure" / "disclosure.yaml").read_text(
                encoding="utf-8"
            )
        )
        packaged["default_executor_tier"] = "local_aligned"
        (conf / "disclosure.yaml").write_text(yaml.safe_dump(packaged), encoding="utf-8")
        assert cli.main(["disclosure", "--list"]) == 0
        assert "undeclared executors resolve to: local_aligned" in capsys.readouterr().out

    def test_broken_override_fails_loudly(self, conf, capsys):
        (conf / "disclosure.yaml").write_text("trust_tiers: []\n", encoding="utf-8")
        assert cli.main(["disclosure", "--list"]) == 1
        assert "disclosure rules are invalid" in capsys.readouterr().out

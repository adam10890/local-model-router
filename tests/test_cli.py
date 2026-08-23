"""Tests for the CLI: parser dispatch, config-check, doctor (hermetic)."""
from __future__ import annotations

import importlib
import json
import sys
import textwrap
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_model_router import cli  # noqa: E402

_CONFIG = textwrap.dedent("""
    active_slots:
      - id: slot_chat
        host: localhost
        port: 8080
        role: chat
        enabled: true
      - id: slot_off
        host: localhost
        port: 8081
        role: utility
        enabled: false
    global:
      backend: remote
""")


def _write_config(tmp_path, monkeypatch):
    config = tmp_path / "llama_cpp_servers.yaml"
    config.write_text(_CONFIG, encoding="utf-8")
    monkeypatch.setenv("A0_LMM_ROUTER_CONFIG", str(config))
    return config


def test_parser_knows_all_commands():
    parser = cli.build_parser()
    for command in ["serve", "doctor", "list-models", "test-route", "config-check"]:
        args = parser.parse_args([command])
        assert args.command == command


def test_default_command_is_serve():
    parser = cli.build_parser()
    args = parser.parse_args([])
    assert args.command is None  # main() maps None -> serve
    assert cli._COMMANDS["serve"] is cli.cmd_serve


def test_parser_accepts_model_evaluation_command():
    args = cli.build_parser().parse_args(
        ["evaluate-models", "--base-url", "http://127.0.0.1:9000"]
    )

    assert args.command == "evaluate-models"
    assert args.force is False


def test_config_check_reports_invalid_upstream_capacity(tmp_path, monkeypatch, capsys):
    _write_config(tmp_path, monkeypatch)
    (tmp_path / "upstreams.yaml").write_text(
        "upstreams:\n"
        "  - name: broken\n"
        "    type: openai_compatible\n"
        "    base_url: http://localhost:9999/v1\n"
        "    enabled: true\n"
        "    max_queue: 2\n",
        encoding="utf-8",
    )

    assert cli.cmd_config_check(cli.build_parser().parse_args(["config-check"])) == 1
    assert "max_queue requires max_active" in capsys.readouterr().out


def test_setup_plan_accepts_windows_utf8_bom(tmp_path, monkeypatch, capsys):
    plan = tmp_path / "plan.json"
    plan.write_bytes('{"backend":"cpu"}'.encode("utf-8-sig"))

    class _Engine:
        def apply(self, payload):
            assert payload == {"backend": "cpu", "confirm_download": True, "confirm_write": True}
            return {"ok": True}

    monkeypatch.setattr(cli, "_setup_engine", lambda: _Engine())
    assert cli.main(["setup", "--plan", str(plan), "--yes"]) == 0
    assert '"ok": true' in capsys.readouterr().out


def test_config_check_ok(tmp_path, monkeypatch, capsys):
    _write_config(tmp_path, monkeypatch)
    rc = cli.main(["config-check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 slot(s) defined, 1 enabled" in out
    assert "slot_chat" in out


def test_config_check_fails_on_bad_yaml(tmp_path, monkeypatch, capsys):
    config = tmp_path / "llama_cpp_servers.yaml"
    config.write_text("active_slots: [unclosed", encoding="utf-8")
    monkeypatch.setenv("A0_LMM_ROUTER_CONFIG", str(config))
    rc = cli.main(["config-check"])
    assert rc == 1
    assert "FAIL" in capsys.readouterr().out


def test_doctor_reports_unreachable_slots(tmp_path, monkeypatch, capsys):
    _write_config(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_probe", lambda url: False)
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] slot reachable: slot_chat" in out
    assert "[PASS] config parses" in out
    assert "is the llama.cpp fleet running" in out


def test_doctor_passes_with_reachable_fleet(tmp_path, monkeypatch, capsys):
    _write_config(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_probe", lambda url: True)
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "all checks passed" in out


def test_doctor_fails_when_aiohttp_lacks_client_session(tmp_path, monkeypatch, capsys):
    _write_config(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_probe", lambda url: True)
    real_import = importlib.import_module

    def import_module(name):
        return types.ModuleType("aiohttp") if name == "aiohttp" else real_import(name)

    monkeypatch.setattr(cli.importlib, "import_module", import_module)
    rc = cli.main(["doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    check = next(row for row in payload["checks"] if row["code"] == "dependency_aiohttp")

    assert rc == 1
    assert check == {
        "code": "dependency_aiohttp",
        "status": "fail",
        "severity": "blocking",
        "label": "dependency: aiohttp",
        "detail": "required capability unavailable: aiohttp.ClientSession",
        "remediation": "Reinstall aiohttp in the Imperium Python environment",
    }


def test_doctor_sanitizes_dependency_import_errors(tmp_path, monkeypatch, capsys):
    _write_config(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_probe", lambda url: True)
    real_import = importlib.import_module

    def import_module(name):
        if name == "aiohttp":
            raise ImportError("C:\\secret\\broken-site-packages")
        return real_import(name)

    monkeypatch.setattr(cli.importlib, "import_module", import_module)
    rc = cli.main(["doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    check = next(row for row in payload["checks"] if row["code"] == "dependency_aiohttp")

    assert rc == 1
    assert check["detail"] == "required capability unavailable: aiohttp.ClientSession"
    assert "secret" not in json.dumps(check).lower()
    assert check["remediation"] == "Reinstall aiohttp in the Imperium Python environment"


def test_test_route_resolves_alias(tmp_path, monkeypatch, capsys):
    _write_config(tmp_path, monkeypatch)

    class _Decision:
        no_slot_available = True

        def model_dump(self):
            return {"no_slot_available": True}

    class _Handler:
        def __init__(self, observer, upstream_rows_fn=None):
            pass

        async def handle(self, intent):
            assert intent.role == "utility"
            return _Decision()

    import local_model_router.service.routing_intent as ri

    monkeypatch.setattr(ri, "RoutingIntentHandler", _Handler)
    rc = cli.main(["test-route", "--model", "coder"])
    out = capsys.readouterr().out
    assert "alias 'coder' -> role utility" in out
    assert rc == 2  # no slot available in this hermetic run

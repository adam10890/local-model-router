"""Tests for the CLI: parser dispatch, config-check, doctor (hermetic)."""
from __future__ import annotations

import sys
import textwrap
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


def test_test_route_resolves_alias(tmp_path, monkeypatch, capsys):
    _write_config(tmp_path, monkeypatch)

    class _Decision:
        no_slot_available = True

        def model_dump(self):
            return {"no_slot_available": True}

    class _Handler:
        def __init__(self, observer):
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

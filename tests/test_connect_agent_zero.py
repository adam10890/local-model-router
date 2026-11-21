"""Hermetic checks for scripts/connect_agent_zero.py (no docker, router, or network)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

_SPEC = importlib.util.spec_from_file_location(
    "connect_agent_zero", Path(__file__).resolve().parents[1] / "scripts" / "connect_agent_zero.py"
)
cz = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cz)


def _conn(name: str, ready: bool = True) -> dict:
    return {
        "name": name,
        "model": "ornith",
        "client_model": "local",
        "base_url": f"http://host.docker.internal:9000/harnesses/agent_zero/{name}/v1",
        "capabilities": {"tools": True, "vision": False, "json_mode": True},
        "readiness": {"model_ready": ready},
    }


def _harness(ready: bool = True, auth: bool = False) -> dict:
    return {
        "harness_id": "agent_zero",
        "authentication_required": auth,
        "connections": [_conn("chat", ready), _conn("utility", ready)],
    }


def _usr(tmp_path: Path) -> Path:
    plugin = tmp_path / "plugins" / "_model_config"
    plugin.mkdir(parents=True)
    (plugin / "presets.yaml").write_text(
        yaml.safe_dump([{"name": "Default", "chat": {"provider": "openrouter", "name": "x"}}]),
        encoding="utf-8",
    )
    return tmp_path


def test_preset_uses_router_urls_and_slot_context():
    preset = cz.build_preset(_harness(), {"ornith": 131072})
    assert preset["chat"]["api_base"].endswith("/harnesses/agent_zero/chat/v1")
    assert preset["utility"]["api_base"].endswith("/harnesses/agent_zero/utility/v1")
    assert preset["chat"]["ctx_length"] == 131072
    assert preset["chat"]["provider"] == "other" and preset["chat"]["vision"] is False


def test_unknown_slot_context_falls_back_conservatively():
    assert cz.build_preset(_harness(), {})["chat"]["ctx_length"] == cz.DEFAULT_CTX


def test_run_is_idempotent_and_keeps_other_presets(tmp_path):
    usr = _usr(tmp_path)
    for _ in range(2):
        cz.run(_harness(), {"ornith": 131072}, usr)
    presets = yaml.safe_load((usr / "plugins/_model_config/presets.yaml").read_text(encoding="utf-8"))
    assert [p["name"] for p in presets] == ["Default", cz.PRESET_NAME]
    assert (usr / "plugins/_model_config/presets.yaml.pre-imperium.bak").is_file()


def test_selects_only_when_ready(tmp_path):
    usr = _usr(tmp_path)
    cfg = usr / "plugins/_model_config/config.json"
    assert cz.run(_harness(ready=False), {}, usr)["selected"] is False
    assert not cfg.exists()
    cfg.write_text(json.dumps({"keep": 1}), encoding="utf-8")
    assert cz.run(_harness(ready=True), {}, usr)["selected"] is True
    assert json.loads(cfg.read_text(encoding="utf-8")) == {"keep": 1, "model_preset": cz.PRESET_NAME}


def test_dry_run_and_no_select_write_nothing_they_should_not(tmp_path):
    usr = _usr(tmp_path)
    before = (usr / "plugins/_model_config/presets.yaml").read_text(encoding="utf-8")
    cz.run(_harness(), {}, usr, dry_run=True)
    assert (usr / "plugins/_model_config/presets.yaml").read_text(encoding="utf-8") == before
    cz.run(_harness(), {}, usr, select=False)
    assert not (usr / "plugins/_model_config/config.json").exists()


def test_api_key_set_when_missing_but_never_overwritten(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ROOT=1\nAPI_KEY_OTHER=\n", encoding="utf-8")
    assert cz.ensure_api_key(env) is True
    assert env.read_text(encoding="utf-8") == "ROOT=1\nAPI_KEY_OTHER=local\n"
    env.write_text("API_KEY_OTHER=sk-real\n", encoding="utf-8")
    assert cz.ensure_api_key(env) is False
    assert env.read_text(encoding="utf-8") == "API_KEY_OTHER=sk-real\n"


def test_no_key_written_when_router_requires_auth(tmp_path):
    usr = _usr(tmp_path)
    assert cz.run(_harness(auth=True), {}, usr)["key_written"] is False
    assert not (usr / ".env").exists()

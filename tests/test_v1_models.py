"""Tests for GET /v1/models aggregation."""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

from starlette.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_model_router.service.app import create_app  # noqa: E402

_CONFIG = textwrap.dedent("""
    active_slots:
      - id: slot_router
        host: localhost
        port: 8080
        role: chat
        enabled: true
        router_mode: true
      - id: slot_disabled
        host: localhost
        port: 8099
        role: utility
        enabled: false
    global:
      backend: remote
      lmm_hosts:
        chat: localhost:8080
""")


def _write_config(tmp_path):
    config = tmp_path / "llama_cpp_servers.yaml"
    config.write_text(_CONFIG, encoding="utf-8")
    return str(config)


async def _fake_fetch(base_url: str):
    return [
        {"id": "chat", "created": 123, "meta": {"n_ctx": 131072}},
        {"id": "gemma-4-12b-it-Q4_K_M", "meta": {"n_ctx": 65536}},
        {"id": ""},  # ignored
        "not-a-dict",  # ignored
    ]


async def _failing_fetch(base_url: str):
    return None


def test_v1_models_lists_aliases_and_slot_models(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    app = create_app(_write_config(tmp_path), models_fetch=_fake_fetch)
    client = TestClient(app)

    resp = client.get("/v1/models")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["object"] == "list"

    by_id = {row["id"]: row for row in payload["data"]}

    # stable aliases are always present
    assert by_id["auto"]["meta"]["kind"] == "alias"
    assert by_id["coder"]["meta"]["maps_to_role"] == "utility"
    assert by_id["fast"]["owned_by"] == "local-model-router"

    # live slot models come through with slot metadata
    assert by_id["gemma-4-12b-it-Q4_K_M"]["meta"]["kind"] == "slot_model"
    assert by_id["gemma-4-12b-it-Q4_K_M"]["meta"]["slot_id"] == "slot_router"
    assert by_id["gemma-4-12b-it-Q4_K_M"]["meta"]["n_ctx"] == 65536
    assert by_id["gemma-4-12b-it-Q4_K_M"]["context_size"] == 65536
    assert by_id["gemma-4-12b-it-Q4_K_M"]["capabilities"]["json_mode"] is True

    # an alias name reported by the slot does not duplicate the alias entry;
    # instead the alias gains live serving metadata
    assert by_id["chat"]["meta"]["kind"] == "alias"
    assert by_id["chat"]["meta"]["live"]["slot_id"] == "slot_router"
    assert by_id["chat"]["meta"]["live"]["n_ctx"] == 131072
    assert by_id["auto"]["capabilities"]["auto_route"] is True

    # disabled slots are not probed (only one slot model id present)
    slot_models = [r for r in payload["data"] if r["meta"].get("kind") == "slot_model"]
    assert {r["meta"]["slot_id"] for r in slot_models} == {"slot_router"}


def test_v1_models_survives_unreachable_slots(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    app = create_app(_write_config(tmp_path), models_fetch=_failing_fetch)
    client = TestClient(app)

    resp = client.get("/v1/models")
    assert resp.status_code == 200
    payload = resp.json()
    kinds = {row["meta"]["kind"] for row in payload["data"]}
    assert kinds == {"alias"}


def test_v1_models_requires_key_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("A0_LMM_ROUTER_API_KEY", "sekret")
    app = create_app(_write_config(tmp_path), models_fetch=_fake_fetch)
    client = TestClient(app)

    assert client.get("/v1/models").status_code == 401
    ok = client.get("/v1/models", headers={"Authorization": "Bearer sekret"})
    assert ok.status_code == 200

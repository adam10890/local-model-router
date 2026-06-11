"""Tests for the Cookbook: GGUF header parsing, fit math, and the endpoint.

Hermetic: synthetic GGUF files are built in tmp_path; no GPU, no fleet.
"""
from __future__ import annotations

import struct

from starlette.testclient import TestClient


# ── synthetic GGUF builder ──────────────────────────────────────────────────

_T_U32 = 4
_T_STR = 8


def _enc_str(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _enc_kv(key: str, vtype: int, value) -> bytes:
    out = _enc_str(key) + struct.pack("<I", vtype)
    if vtype == _T_U32:
        out += struct.pack("<I", value)
    elif vtype == _T_STR:
        out += _enc_str(value)
    else:
        raise ValueError(vtype)
    return out


def _write_gguf(path, *, arch="llama", n_layers=32, n_embd=4096, n_head=32,
                n_head_kv=8, n_ctx=32768, file_type=15, pad_to=0):
    kvs = [
        _enc_kv("general.architecture", _T_STR, arch),
        _enc_kv("general.name", _T_STR, "test-model"),
        _enc_kv("general.file_type", _T_U32, file_type),
        _enc_kv(f"{arch}.block_count", _T_U32, n_layers),
        _enc_kv(f"{arch}.embedding_length", _T_U32, n_embd),
        _enc_kv(f"{arch}.attention.head_count", _T_U32, n_head),
        _enc_kv(f"{arch}.attention.head_count_kv", _T_U32, n_head_kv),
        _enc_kv(f"{arch}.context_length", _T_U32, n_ctx),
        # tokenizer key marks where the parser must stop; garbage value after
        # the key proves it never reads past it.
        _enc_str("tokenizer.ggml.tokens") + b"\xff\xff\xff\xff",
    ]
    header = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", len(kvs))
    blob = header + b"".join(kvs)
    if pad_to > len(blob):
        blob += b"\x00" * (pad_to - len(blob))
    path.write_bytes(blob)


# ── parser ──────────────────────────────────────────────────────────────────

def test_read_gguf_meta_parses_geometry(tmp_path):
    from local_model_router.cookbook.gguf import read_gguf_meta

    f = tmp_path / "m.gguf"
    _write_gguf(f)
    meta = read_gguf_meta(f)

    assert meta.architecture == "llama"
    assert meta.name == "test-model"
    assert meta.quant == "Q4_K_M"
    assert meta.n_layers == 32
    assert meta.n_head_kv == 8
    assert meta.head_dim == 128
    assert meta.n_ctx_train == 32768
    # K+V * layers * kv-heads * head_dim * 2 bytes = 2*32*8*128*2
    assert meta.kv_bytes_per_token() == 131072


def test_read_gguf_meta_rejects_non_gguf(tmp_path):
    from local_model_router.cookbook.gguf import read_gguf_meta

    f = tmp_path / "not.gguf"
    f.write_bytes(b"NOPE" + b"\x00" * 64)
    try:
        read_gguf_meta(f)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "magic" in str(exc)


# ── scan + engine ───────────────────────────────────────────────────────────

def test_scan_groups_shards_and_hints_roles(tmp_path):
    from local_model_router.cookbook.engine import scan_models_dir

    (tmp_path / "chat").mkdir()
    _write_gguf(tmp_path / "chat" / "big-00001-of-00002.gguf", pad_to=1024)
    (tmp_path / "chat" / "big-00002-of-00002.gguf").write_bytes(b"\x00" * 2048)
    _write_gguf(tmp_path / "solo.gguf", pad_to=512)

    entries = scan_models_dir(str(tmp_path))

    assert len(entries) == 2
    shard_entry = next(e for e in entries if "big" in e["path"].name)
    assert shard_entry["size_bytes"] == 1024 + 2048
    assert shard_entry["role_hint"] == "chat"
    solo = next(e for e in entries if e["path"].name == "solo.gguf")
    assert solo["role_hint"] is None


def test_build_report_fit_and_recommendations(tmp_path):
    from local_model_router.cookbook.engine import build_report

    (tmp_path / "chat").mkdir()
    (tmp_path / "utility").mkdir()
    _write_gguf(tmp_path / "chat" / "chat-q4_k_m.gguf", pad_to=8 * 1024 * 1024)
    _write_gguf(tmp_path / "utility" / "small-q4_k_m.gguf", n_layers=16,
                n_embd=2048, n_head=16, n_head_kv=4, pad_to=2 * 1024 * 1024)

    hardware = {
        "gpus": [{"name": "RTX 4090", "vram_gb": 24, "enabled": True}],
        "ram": {"available_gb": 32},
    }
    policy = {"vram_safety_margin_gb": 2.5, "role_min_ctx": {"chat": 16384, "utility": 8192}}

    report = build_report(str(tmp_path), hardware, policy)

    assert report["model_count"] == 2
    assert report["hardware"]["vram_budget_gb"] == 21.5
    by_file = {m["file"]: m for m in report["models"]}
    chat_model = by_file["chat-q4_k_m.gguf"]
    assert chat_model["fit"] == "full_gpu"
    # tiny file, huge budget -> ctx capped by training context
    assert chat_model["max_ctx_fit"] == 32768
    assert chat_model["confidence"] in ("medium", "high")
    assert any("KV cache" in r for r in chat_model["reasons"])
    # folder hints pin models to their roles
    assert report["recommendations"]["chat"]["file"] == "chat-q4_k_m.gguf"
    assert report["recommendations"]["utility"]["file"] == "small-q4_k_m.gguf"


def test_build_report_flags_too_big_models(tmp_path):
    from local_model_router.cookbook.engine import build_report

    _write_gguf(tmp_path / "huge.gguf", pad_to=4 * 1024 * 1024)
    hardware = {"gpus": [{"vram_gb": 0}], "ram": {"available_gb": 0}}

    report = build_report(str(tmp_path), hardware, {})

    assert report["models"][0]["fit"] == "too_big"
    assert "chat" not in report["recommendations"]


# ── endpoint ────────────────────────────────────────────────────────────────

_CONFIG_TMPL = """\
active_slots:
  - id: chat
    port: 8080
    host: localhost
    role: chat
    enabled: true
    model_id: chat-model
    context_size: 65536
global:
  backend: remote
  models_dir: '{models_dir}'
hardware:
  gpus:
    - name: RTX 4090
      vram_gb: 24
      enabled: true
  ram:
    available_gb: 32
context_policy:
  vram_safety_margin_gb: 2.5
  role_min_ctx:
    chat: 16384
"""


def _client(tmp_path, monkeypatch, models_dir, api_key=None):
    from local_model_router.helpers.llama_cpp_manager import BackendManager
    from local_model_router.service.app import create_app

    BackendManager._instance = None
    cfg = tmp_path / "llama_cpp_servers.yaml"
    cfg.write_text(
        _CONFIG_TMPL.format(models_dir=str(models_dir).replace("\\", "\\\\")),
        encoding="utf-8",
    )

    async def health_probe(url, timeout):
        return {"ok": True}

    monkeypatch.setattr(
        "local_model_router.helpers.smart_router.health._aiohttp_probe",
        health_probe,
    )
    monkeypatch.delenv("LLAMA_MODELS_DIR", raising=False)
    if api_key is not None:
        monkeypatch.setenv("A0_LMM_ROUTER_API_KEY", api_key)
    else:
        monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    return TestClient(create_app(str(cfg))), BackendManager


def test_cookbook_endpoint_returns_report(tmp_path, monkeypatch):
    models = tmp_path / "models" / "chat"
    models.mkdir(parents=True)
    _write_gguf(models / "chat.gguf", pad_to=1024 * 1024)
    client, manager_cls = _client(tmp_path, monkeypatch, tmp_path / "models")

    resp = client.get("/cookbook")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["model_count"] == 1
    assert body["recommendations"]["chat"]["file"] == "chat.gguf"
    manager_cls._instance = None


def test_cookbook_endpoint_reports_missing_models_dir(tmp_path, monkeypatch):
    client, manager_cls = _client(tmp_path, monkeypatch, tmp_path / "does-not-exist")

    resp = client.get("/cookbook")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "models_dir_not_configured"
    manager_cls._instance = None


def test_cookbook_endpoint_requires_auth_when_key_set(tmp_path, monkeypatch):
    models = tmp_path / "models"
    models.mkdir()
    client, manager_cls = _client(tmp_path, monkeypatch, models, api_key="local-secret")

    assert client.get("/cookbook").status_code == 401
    assert client.get(
        "/cookbook", headers={"Authorization": "Bearer local-secret"}
    ).status_code == 200
    manager_cls._instance = None

"""Phase 8: wiring the usage ledger into the upstream forwarding path.

_record_upstream_usage lives in local_model_router/service/app.py (module
level, not nested in create_app) and is called from forward_to_upstream —
the single choke point both the explicit-upstream-model-match path and the
auto-upstream-fallback path return through — right where the real upstream
name (UpstreamConfig.name) and the real response usage are both known.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
from starlette.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_model_router.helpers import usage_ledger  # noqa: E402
from local_model_router.service.app import _record_upstream_usage, create_app  # noqa: E402


def _isolated_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(usage_ledger, "LEDGER_PATH", tmp_path / "usage_ledger.jsonl")
    usage_ledger._cache.clear()
    monkeypatch.setattr(usage_ledger, "_last_prune_day", None)


# ---------------------------------------------------------------------------
# _record_upstream_usage unit tests
# ---------------------------------------------------------------------------

def test_record_upstream_usage_writes_ledger_event(monkeypatch, tmp_path):
    _isolated_ledger(monkeypatch, tmp_path)
    _record_upstream_usage("ollama_cloud", {"prompt_tokens": 10, "completion_tokens": 5}, "llama3.3:70b")
    events = usage_ledger.all_events()
    assert len(events) == 1
    assert events[0]["provider_id"] == "ollama_cloud"
    assert events[0]["tokens_in"] == 10
    assert events[0]["tokens_out"] == 5
    assert events[0]["model"] == "llama3.3:70b"
    assert events[0]["source"] == "proxy"


def test_record_upstream_usage_no_name_records_nothing(monkeypatch, tmp_path):
    _isolated_ledger(monkeypatch, tmp_path)
    _record_upstream_usage(None, {"prompt_tokens": 10, "completion_tokens": 5}, "m")
    _record_upstream_usage("", {"prompt_tokens": 10, "completion_tokens": 5}, "m")
    assert usage_ledger.all_events() == []


def test_record_upstream_usage_empty_usage_records_nothing(monkeypatch, tmp_path):
    _isolated_ledger(monkeypatch, tmp_path)
    _record_upstream_usage("ollama_cloud", {}, "m")
    _record_upstream_usage("ollama_cloud", None, "m")
    _record_upstream_usage("ollama_cloud", {"prompt_tokens": 0, "completion_tokens": 0}, "m")
    assert usage_ledger.all_events() == []


def test_record_upstream_usage_swallows_ledger_errors(monkeypatch, tmp_path):
    _isolated_ledger(monkeypatch, tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(usage_ledger, "record_usage", _boom)
    # Must not raise.
    _record_upstream_usage("ollama_cloud", {"prompt_tokens": 1, "completion_tokens": 1}, "m")


# ---------------------------------------------------------------------------
# End-to-end: forwarding a completion to a declared upstream lands in the ledger
# ---------------------------------------------------------------------------

_FLEET_CONFIG = textwrap.dedent("""
    active_slots:
      - id: slot_router
        host: localhost
        port: 8080
        role: chat
        enabled: true
        router_mode: true
    global:
      backend: remote
""")

_UPSTREAMS = textwrap.dedent("""
    upstreams:
      - name: ollama_cloud
        type: openai_compatible
        base_url: http://localhost:11434/v1
        enabled: true
""")


def _write(tmp_path, name, content):
    target = tmp_path / name
    target.write_text(content, encoding="utf-8")
    return str(target)


def _make_app(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    return create_app(
        _write(tmp_path, "llama_cpp_servers.yaml", _FLEET_CONFIG),
        models_fetch=None,
        upstreams_path=_write(tmp_path, "upstreams.yaml", _UPSTREAMS),
        apps_path=str(tmp_path / "missing-apps.yaml"),
    )


def test_chat_completions_forward_records_usage_in_ledger(tmp_path, monkeypatch):
    import aiohttp

    _isolated_ledger(monkeypatch, tmp_path)

    class FakeResponse:
        status = 200

        async def json(self, content_type=None):
            return {
                "choices": [{"message": {"content": "from-ollama"}}],
                "model": "llama3.3:70b",
                "usage": {"prompt_tokens": 12, "completion_tokens": 8},
            }

        def release(self):
            return None

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        def post(self, url, **kwargs):
            class _Ctx:
                async def __aenter__(_self):
                    return FakeResponse()

                async def __aexit__(_self, *exc):
                    return None

            return _Ctx()

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    client = TestClient(_make_app(tmp_path, monkeypatch))

    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "ollama_cloud/llama3.3:70b",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert resp.status_code == 200

    events = usage_ledger.all_events()
    assert len(events) == 1
    assert events[0]["provider_id"] == "ollama_cloud"
    assert events[0]["tokens_in"] == 12
    assert events[0]["tokens_out"] == 8

    totals = usage_ledger.window_totals("ollama_cloud", 3600)
    assert totals == {"tokens": 20, "requests": 1}

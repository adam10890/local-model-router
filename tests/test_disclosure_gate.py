"""The runtime disclosure gate at the upstream forwarding choke point.

Default posture is observe-only: the verdict is reported in headers and the
forward happens unchanged. Enforcement is opt-in and must be explicit.
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

from local_model_router.service.app import create_app  # noqa: E402

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
      - name: public
        type: openai_compatible
        base_url: http://localhost:11434/v1
        enabled: true
        trust_tier: other_provider
      - name: trusted
        type: openai_compatible
        base_url: http://localhost:11435/v1
        enabled: true
        trust_tier: local_uncensored
      - name: undeclared
        type: openai_compatible
        base_url: http://localhost:11436/v1
        enabled: true
""")

_SECRET = "sk-abcdefghijklmnopqrstuvwx"


def _write(tmp_path, name, content):
    target = tmp_path / name
    target.write_text(content, encoding="utf-8")
    return str(target)


@pytest.fixture()
def fake_upstream(monkeypatch):
    """Stub aiohttp so no request leaves the test process."""
    import aiohttp

    calls = []

    class FakeResponse:
        status = 200

        async def json(self, content_type=None):
            return {"choices": [{"message": {"content": "ok"}}], "model": "m"}

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
            calls.append({"url": url, "kwargs": kwargs})

            class _Ctx:
                async def __aenter__(_self):
                    return FakeResponse()

                async def __aexit__(_self, *exc):
                    return None

            return _Ctx()

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    return calls


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    monkeypatch.delenv("A0_LMM_ROUTER_DISCLOSURE_ENFORCE", raising=False)
    return TestClient(
        create_app(
            _write(tmp_path, "llama_cpp_servers.yaml", _FLEET_CONFIG),
            upstreams_path=_write(tmp_path, "upstreams.yaml", _UPSTREAMS),
            apps_path=str(tmp_path / "missing-apps.yaml"),
        )
    )


def _chat(client, upstream, content):
    return client.post(
        "/v1/chat/completions",
        json={"model": f"{upstream}/m", "messages": [{"role": "user", "content": content}]},
    )


# ---------------------------------------------------------------------------
# Observe-only default
# ---------------------------------------------------------------------------

class TestObserveOnlyDefault:
    def test_allowed_forward_is_annotated(self, client, fake_upstream):
        resp = _chat(client, "public", "Write a boilerplate parser stub.")
        assert resp.status_code == 200
        assert resp.headers["x-a0-router-trust-tier"] == "other_provider"
        assert resp.headers["x-a0-router-disclosure"] == "allow"
        assert resp.headers["x-a0-router-disclosure-class"] == "generic_scaffold"

    def test_denied_forward_still_goes_through_by_default(self, client, fake_upstream):
        resp = _chat(client, "public", f"Rotate the fleet api_key: {_SECRET}")
        assert resp.status_code == 200  # observe-only: reported, not blocked
        assert resp.headers["x-a0-router-disclosure"] == "deny"
        assert len(fake_upstream) == 1

    def test_trusted_upstream_reports_its_tier(self, client, fake_upstream):
        resp = _chat(client, "trusted", "Summarize the fleet telemetry inventory.")
        assert resp.status_code == 200
        assert resp.headers["x-a0-router-trust-tier"] == "local_uncensored"
        assert resp.headers["x-a0-router-disclosure"] == "allow"

    def test_undeclared_upstream_reports_the_least_trusted_tier(self, client, fake_upstream):
        resp = _chat(client, "undeclared", "Write a boilerplate parser stub.")
        assert resp.status_code == 200
        assert resp.headers["x-a0-router-trust-tier"] == "other_provider"

    def test_headers_never_carry_matched_text(self, client, fake_upstream):
        resp = _chat(client, "public", f"api_key: {_SECRET}")
        assert _SECRET not in str(dict(resp.headers))
        assert _SECRET not in resp.text


# ---------------------------------------------------------------------------
# Opt-in enforcement
# ---------------------------------------------------------------------------

class TestEnforcement:
    def test_denied_forward_is_blocked_when_enforcing(self, client, fake_upstream, monkeypatch):
        monkeypatch.setenv("A0_LMM_ROUTER_DISCLOSURE_ENFORCE", "1")
        resp = _chat(client, "public", "Summarize the fleet telemetry inventory for the operator.")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "disclosure_policy_violation"
        assert fake_upstream == []  # nothing left the process

    def test_allowed_forward_is_unaffected_by_enforcement(self, client, fake_upstream, monkeypatch):
        monkeypatch.setenv("A0_LMM_ROUTER_DISCLOSURE_ENFORCE", "1")
        resp = _chat(client, "public", "Write a boilerplate parser stub.")
        assert resp.status_code == 200
        assert len(fake_upstream) == 1

    def test_trusted_upstream_still_receives_sensitive_content(
        self, client, fake_upstream, monkeypatch
    ):
        monkeypatch.setenv("A0_LMM_ROUTER_DISCLOSURE_ENFORCE", "1")
        resp = _chat(client, "trusted", "Summarize the fleet telemetry inventory for the operator.")
        assert resp.status_code == 200

    def test_denial_body_explains_without_quoting(self, client, fake_upstream, monkeypatch):
        monkeypatch.setenv("A0_LMM_ROUTER_DISCLOSURE_ENFORCE", "1")
        resp = _chat(client, "public", f"Fleet telemetry inventory. api_key: {_SECRET}")
        assert resp.status_code == 403
        body = resp.text
        assert _SECRET not in body
        payload = resp.json()["disclosure"]
        assert payload["outcome"] == "deny"
        assert "executor_below_content_cap" in payload["reason_codes"]

    def test_enforcement_flag_is_read_per_request(self, client, fake_upstream, monkeypatch):
        content = "Summarize the fleet telemetry inventory for the operator."
        assert _chat(client, "public", content).status_code == 200
        monkeypatch.setenv("A0_LMM_ROUTER_DISCLOSURE_ENFORCE", "1")
        assert _chat(client, "public", content).status_code == 403
        monkeypatch.setenv("A0_LMM_ROUTER_DISCLOSURE_ENFORCE", "0")
        assert _chat(client, "public", content).status_code == 200


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------

class TestResilience:
    def test_broken_override_falls_back_to_packaged_rules(self, tmp_path, monkeypatch, fake_upstream):
        monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
        conf_path = _write(tmp_path, "llama_cpp_servers.yaml", _FLEET_CONFIG)
        _write(tmp_path, "disclosure.yaml", "trust_tiers: []\n")
        app = create_app(
            conf_path,
            upstreams_path=_write(tmp_path, "upstreams.yaml", _UPSTREAMS),
            apps_path=str(tmp_path / "missing-apps.yaml"),
        )
        resp = _chat(TestClient(app), "public", "Write a boilerplate parser stub.")
        assert resp.status_code == 200
        assert resp.headers["x-a0-router-disclosure"] == "allow"

    @pytest.mark.parametrize(
        "declared,expected",
        [("local_uncensored", "local_uncensored"), (None, "other_provider")],
    )
    def test_routing_decision_names_the_selected_slot_tier(
        self, tmp_path, declared, expected
    ):
        """A completed routing decision says how far the content travelled.

        Undeclared slots report the least-trusted tier, the same fail-closed
        rule the gate applies.
        """
        import asyncio

        from local_model_router.helpers.llama_cpp_manager import BackendManager
        from local_model_router.service.routing_intent import (
            RoutingIntentHandler,
            RoutingIntentRequest,
        )

        tier_line = f"    trust_tier: {declared}\n" if declared else ""
        config = (
            "active_slots:\n"
            "  - id: chat\n"
            "    host: localhost\n"
            "    port: 8080\n"
            "    role: chat\n"
            "    enabled: true\n"
            f"{tier_line}"
            "global:\n"
            "  backend: remote\n"
        )
        BackendManager._instance = None
        manager = BackendManager(_write(tmp_path, "llama_cpp_servers.yaml", config))

        class StubChecker:
            async def check_async(self, slot_config):
                return "healthy"

            def check(self, slot_config):
                return "healthy"

        manager._health_checker = StubChecker()

        class StubObserver:
            def _make_manager(self):
                return manager

        decision = asyncio.run(
            RoutingIntentHandler(StubObserver()).handle(
                RoutingIntentRequest.model_validate({"task_type": "chat"})
            )
        )
        assert decision.selected_slot_id == "chat"
        assert f"executor_tier:{expected}" in decision.reason_codes

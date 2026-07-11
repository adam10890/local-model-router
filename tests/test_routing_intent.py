"""
Tests for POST /routing/request — Agent Client Contract dry-run endpoint.

All tests use stub health checkers; no real llama.cpp servers needed.
Slot IDs must match DEFAULT_CHAINS keys for routing to succeed (see conftest comments).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "local_model_router"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from starlette.testclient import TestClient  # noqa: E402

# Slot IDs match DEFAULT_CHAINS["chat"] = ["chat", "utility", ...]
_ROUTING_CONFIG = """\
active_slots:
  - id: chat
    port: 8080
    host: localhost
    role: chat
    enabled: true
    model_id: mistral-7b-q4
  - id: utility
    port: 8088
    host: localhost
    role: utility
    enabled: true
global:
  backend: remote
"""

_EMPTY_CONFIG = "active_slots: []\nglobal:\n  backend: remote\n"


def _make_client(tmp_path, yaml_content=_ROUTING_CONFIG, health_result="healthy"):
    """Return a TestClient with a stub health checker injected."""
    from local_model_router.service.app import create_app
    from local_model_router.service.observer import ObserverBackend
    from local_model_router.helpers.llama_cpp_manager import BackendManager

    cfg = tmp_path / "llama_cpp_servers.yaml"
    cfg.write_text(yaml_content)

    BackendManager._instance = None
    mgr = BackendManager(str(cfg))

    _hr = health_result

    class StubChecker:
        async def check_async(self, config):
            return _hr
        def check(self, config):
            return _hr

    mgr._health_checker = StubChecker()

    obs = ObserverBackend(str(cfg))
    obs._make_manager = lambda: mgr

    app = create_app(str(cfg))
    # Wire the observer with stub into the app's intent handler
    from local_model_router.service.routing_intent import RoutingIntentHandler
    # Rebuild the app with the stubbed observer
    from starlette.applications import Starlette
    from starlette.routing import Route

    from local_model_router.service.app import _VERSION, _SERVICE_NAME
    from pydantic import ValidationError
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    intent_handler = RoutingIntentHandler(obs)

    async def routing_request(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        try:
            from local_model_router.service.routing_intent import RoutingIntentRequest
            intent = RoutingIntentRequest.model_validate(body)
        except ValidationError as exc:
            import json as _json
            return JSONResponse({"error": "validation_error", "detail": _json.loads(exc.json())}, status_code=422)
        result = await intent_handler.handle(intent)
        return JSONResponse(result.model_dump())

    stub_app = Starlette(routes=[Route("/routing/request", routing_request, methods=["POST"])])
    return TestClient(stub_app, raise_server_exceptions=True), mgr


def _post(client, body):
    return client.post("/routing/request", json=body)


# ---------------------------------------------------------------------------
# Structural guarantees
# ---------------------------------------------------------------------------

class TestDryRunGuarantee:
    def test_response_always_has_dry_run_true(self, tmp_path):
        client, _ = _make_client(tmp_path)
        body = _post(client, {}).json()
        assert body["dry_run"] is True

    def test_response_has_decision_id(self, tmp_path):
        client, _ = _make_client(tmp_path)
        body = _post(client, {}).json()
        assert "decision_id" in body
        assert len(body["decision_id"]) > 0

    def test_response_has_reason_codes_list(self, tmp_path):
        client, _ = _make_client(tmp_path)
        body = _post(client, {}).json()
        assert isinstance(body["reason_codes"], list)

    def test_response_has_warnings_list(self, tmp_path):
        client, _ = _make_client(tmp_path)
        body = _post(client, {}).json()
        assert isinstance(body["warnings"], list)


# ---------------------------------------------------------------------------
# Basic slot selection
# ---------------------------------------------------------------------------

class TestBasicSlotSelection:
    def test_selects_chat_slot_for_chat_task(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"task_type": "chat"}).json()
        assert body["no_slot_available"] is False
        assert body["selected_slot_id"] == "chat"
        assert body["selected_url"] is not None

    def test_role_inferred_from_task_type_coding(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"task_type": "coding"}).json()
        assert body["role"] == "utility"

    def test_role_inferred_from_task_type_embedding(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"task_type": "embedding"}).json()
        assert body["role"] == "embed"

    def test_explicit_role_overrides_task_type(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"task_type": "coding", "role": "chat"}).json()
        assert body["role"] == "chat"

    def test_selected_model_present_when_configured(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {}).json()
        # slot_chat has model_id: mistral-7b-q4 in _ROUTING_CONFIG
        assert body["selected_model"] == "mistral-7b-q4"

    def test_selected_backend_type_present(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {}).json()
        assert body["selected_backend_type"] is not None


# ---------------------------------------------------------------------------
# Role and preferred_slot
# ---------------------------------------------------------------------------

class TestRoleAndPreferredSlot:
    def test_explicit_role_chat(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"role": "chat"}).json()
        assert body["role"] == "chat"
        assert body["no_slot_available"] is False

    def test_preferred_slot_respected(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"preferred_slot": "utility"}).json()
        assert body["selected_slot_id"] == "utility"

    def test_agent_id_echoed_in_response(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"agent_id": "hermes-001"}).json()
        assert body["agent_id"] == "hermes-001"

    def test_agent_type_echoed_in_response(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"agent_type": "hermes"}).json()
        assert body["agent_type"] == "hermes"


# ---------------------------------------------------------------------------
# No slot available
# ---------------------------------------------------------------------------

class TestNoSlotAvailable:
    def test_all_unhealthy_returns_no_slot(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="unhealthy")
        body = _post(client, {}).json()
        assert body["no_slot_available"] is True
        assert body["selected_slot_id"] is None
        assert body["selected_url"] is None

    def test_empty_config_returns_no_slot(self, tmp_path):
        client, _ = _make_client(tmp_path, _EMPTY_CONFIG, health_result="healthy")
        body = _post(client, {}).json()
        assert body["no_slot_available"] is True


# ---------------------------------------------------------------------------
# Privacy and local_only policy
# ---------------------------------------------------------------------------

class TestPrivacyPolicy:
    def test_local_only_true_sets_enforced_flag(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"local_only": True}).json()
        assert body["local_only_enforced"] is True

    def test_privacy_mode_local_only_sets_enforced_flag(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"privacy_mode": "local_only"}).json()
        assert body["local_only_enforced"] is True

    def test_local_only_false_does_not_enforce(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"local_only": False}).json()
        assert body["local_only_enforced"] is False

    def test_cloud_flags_are_not_part_of_local_router_response(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"cloud_allowed": True}).json()
        assert "cloud_allowed" not in body
        assert not any("cloud_routing" in warning for warning in body["warnings"])


# ---------------------------------------------------------------------------
# Cloud routing warning
# ---------------------------------------------------------------------------

class TestUnknownCloudPolicy:
    def test_cloud_preferred_is_an_unknown_privacy_mode(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"privacy_mode": "cloud_preferred"}).json()
        assert "unknown_privacy_mode:cloud_preferred" in body["warnings"]


# ---------------------------------------------------------------------------
# Capability warnings
# ---------------------------------------------------------------------------

class TestCapabilityWarnings:
    def test_requires_long_context_adds_warning(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"requires_long_context": True}).json()
        assert any("long_context" in w for w in body["warnings"])

    def test_requires_tools_adds_warning(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"requires_tools": True}).json()
        assert any("tool_routing" in w for w in body["warnings"])

    def test_requires_code_execution_adds_warning(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"requires_code_execution": True}).json()
        assert any("code_execution" in w for w in body["warnings"])


# ---------------------------------------------------------------------------
# Unknown values — tolerated with warnings
# ---------------------------------------------------------------------------

class TestUnknownValueTolerance:
    def test_unknown_agent_type_accepted_with_warning(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"agent_type": "my_future_agent"}).json()
        assert body["agent_type"] == "my_future_agent"
        assert any("unknown_agent_type" in w for w in body["warnings"])

    def test_unknown_task_type_falls_back_to_chat(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"task_type": "future_task_type"}).json()
        assert body["role"] == "chat"
        assert any("unknown_task_type" in w for w in body["warnings"])

    def test_unknown_privacy_mode_adds_warning(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"privacy_mode": "super_private"}).json()
        assert any("unknown_privacy_mode" in w for w in body["warnings"])

    def test_unknown_agent_type_does_not_prevent_routing(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"agent_type": "totally_new_agent"}).json()
        assert body["no_slot_available"] is False


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------

class TestMalformedInput:
    def test_invalid_json_returns_400(self, tmp_path):
        from local_model_router.service.app import create_app
        from local_model_router.service.observer import ObserverBackend
        cfg = tmp_path / "c.yaml"
        cfg.write_text(_ROUTING_CONFIG)
        # Use the real app for this test since it covers the JSON parse path
        app = create_app(str(cfg))
        client = TestClient(app)
        resp = client.post(
            "/routing/request",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "invalid_json" in resp.json()["error"]

    def test_negative_estimated_tokens_returns_422(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        resp = client.post("/routing/request", json={"estimated_tokens": -1})
        assert resp.status_code == 422

    def test_empty_body_uses_all_defaults(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {}).json()
        assert body["agent_id"] == "unknown"
        assert body["task_type"] == "chat"
        assert body["role"] == "chat"

    def test_extra_fields_are_ignored(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        # pydantic v2 ignores extra fields by default
        body = _post(client, {"future_field": "value", "another": 42}).json()
        assert "dry_run" in body


# ---------------------------------------------------------------------------
# Response never contains secrets
# ---------------------------------------------------------------------------

class TestNoSecretsInResponse:
    def test_no_api_key_in_response(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {}).json()
        text = str(body)
        assert "api_key" not in text.lower() or "[REDACTED]" in text

    def test_no_token_value_in_response(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {}).json()
        # Response should not contain raw token strings
        assert "should-be-redacted" not in str(body)


# ---------------------------------------------------------------------------
# Schema unit tests (not via HTTP)
# ---------------------------------------------------------------------------

class TestRoutingIntentSchema:
    def test_role_from_task_type_chat(self):
        from local_model_router.service.routing_intent import _role_from_task_type
        assert _role_from_task_type("chat") == "chat"

    def test_role_from_task_type_coding(self):
        from local_model_router.service.routing_intent import _role_from_task_type
        assert _role_from_task_type("coding") == "utility"

    def test_role_from_task_type_embedding(self):
        from local_model_router.service.routing_intent import _role_from_task_type
        assert _role_from_task_type("embedding") == "embed"

    def test_role_from_unknown_task_defaults_to_chat(self):
        from local_model_router.service.routing_intent import _role_from_task_type
        assert _role_from_task_type("zap_widget") == "chat"

    def test_request_defaults_are_sensible(self):
        from local_model_router.service.routing_intent import RoutingIntentRequest
        req = RoutingIntentRequest.model_validate({})
        assert req.agent_id == "unknown"
        assert req.task_type == "chat"
        assert req.local_only is False
        assert not hasattr(req, "cloud_allowed")

    def test_request_validation_error_on_negative_tokens(self):
        from pydantic import ValidationError
        from local_model_router.service.routing_intent import RoutingIntentRequest
        try:
            RoutingIntentRequest.model_validate({"estimated_tokens": -5})
            assert False, "should have raised"
        except ValidationError:
            pass


# ---------------------------------------------------------------------------
# Ranked-candidate order drives the failover chain
# ---------------------------------------------------------------------------

class TestRankedFailoverChain:
    def test_reason_codes_name_ranked_chain(self, tmp_path):
        client, _ = _make_client(tmp_path, health_result="healthy")
        body = _post(client, {"task_type": "chat"}).json()
        assert "failover_chain:ranked" in body["reason_codes"]

    def test_reason_codes_name_config_chain_when_no_candidates(self, tmp_path):
        client, _ = _make_client(tmp_path, yaml_content=_EMPTY_CONFIG, health_result="healthy")
        body = _post(client, {"task_type": "chat"}).json()
        assert "failover_chain:config" in body["reason_codes"]

    def test_walk_falls_to_second_ranked_candidate(self, tmp_path):
        """Top-ranked slot turns unhealthy between ranking and the chain walk;
        the walk must continue to the next ranked candidate and flag fallback."""
        client, mgr = _make_client(tmp_path, health_result="healthy")
        calls = {}

        class FlakyChecker:
            async def check_async(self, config):
                port = config.get("port")
                n = calls.get(port, 0)
                calls[port] = n + 1
                if port == 8080:  # chat slot: healthy at ranking, then down
                    return "healthy" if n == 0 else "unhealthy"
                return "healthy"

            def check(self, config):
                return "healthy"

        mgr._health_checker = FlakyChecker()
        body = _post(client, {"task_type": "chat"}).json()

        assert body["no_slot_available"] is False
        assert body["selected_slot_id"] == "utility"
        assert body["fallback_used"] is True
        assert "primary_slot_unavailable_failover_used" in body["reason_codes"]

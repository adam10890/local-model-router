"""
Tests for the pure failover logic in helpers/smart_router/failover.py.

These tests require no running services, no config files, and no BackendManager.
They validate the data structures and classification functions in isolation.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# classify_failover_reason
# ---------------------------------------------------------------------------

class TestClassifyFailoverReason:
    def _classify(self, exc):
        from local_model_router.helpers.smart_router.failover import classify_failover_reason
        return classify_failover_reason(exc)

    def test_rate_limit_by_status_code(self):
        exc = Exception("HTTP error")
        exc.status_code = 429
        result = self._classify(exc)
        assert result["reason"].value == "rate_limit"
        assert result["status_code"] == 429

    def test_timeout_by_status_code_408(self):
        exc = Exception("request timeout")
        exc.status_code = 408
        result = self._classify(exc)
        assert result["reason"].value == "timeout"

    def test_provider_error_by_status_500(self):
        exc = Exception("internal error")
        exc.status_code = 500
        result = self._classify(exc)
        assert result["reason"].value == "provider_error"

    def test_timeout_by_text(self):
        result = self._classify(Exception("connection timed out after 30s"))
        assert result["reason"].value == "timeout"
        assert result["status_code"] is None

    def test_rate_limit_by_text(self):
        result = self._classify(Exception("rate limit exceeded"))
        assert result["reason"].value == "rate_limit"

    def test_quota_exhausted_by_text(self):
        result = self._classify(Exception("insufficient_quota for this model"))
        assert result["reason"].value == "quota_exhausted"

    def test_provider_error_by_text(self):
        result = self._classify(Exception("connection refused by host"))
        assert result["reason"].value == "provider_error"

    def test_unknown_error(self):
        result = self._classify(Exception("some unexpected error"))
        assert result["reason"].value == "unknown_error"
        assert result["status_code"] is None


# ---------------------------------------------------------------------------
# should_failover
# ---------------------------------------------------------------------------

class TestShouldFailover:
    def _should(self, exc):
        from local_model_router.helpers.smart_router.failover import should_failover
        return should_failover(exc)

    def test_429_triggers_failover(self):
        exc = Exception("rate limited")
        exc.status_code = 429
        assert self._should(exc) is True

    def test_500_triggers_failover(self):
        exc = Exception("server error")
        exc.status_code = 500
        assert self._should(exc) is True

    def test_503_triggers_failover(self):
        exc = Exception("service unavailable")
        exc.status_code = 503
        assert self._should(exc) is True

    def test_404_does_not_trigger_failover(self):
        exc = Exception("not found")
        exc.status_code = 404
        assert self._should(exc) is False

    def test_timeout_text_triggers_failover(self):
        assert self._should(Exception("connection timed out")) is True

    def test_unknown_error_does_not_trigger_failover(self):
        assert self._should(Exception("some unexpected business logic error")) is False


# ---------------------------------------------------------------------------
# get_next_in_chain
# ---------------------------------------------------------------------------

class TestGetNextInChain:
    def _next(self, current, chain):
        from local_model_router.helpers.smart_router.failover import get_next_in_chain
        return get_next_in_chain(current, chain)

    def test_returns_second_when_first_is_current(self):
        assert self._next("chat", ["chat", "utility", "openrouter"]) == "utility"

    def test_returns_third_from_second(self):
        assert self._next("utility", ["chat", "utility", "openrouter"]) == "openrouter"

    def test_returns_none_at_end_of_chain(self):
        assert self._next("openrouter", ["chat", "utility", "openrouter"]) is None

    def test_returns_first_when_current_not_in_chain(self):
        assert self._next("unknown", ["chat", "utility"]) == "chat"

    def test_empty_chain_returns_none(self):
        assert self._next("chat", []) is None

    def test_single_element_chain_at_end_returns_none(self):
        assert self._next("embed", ["embed"]) is None


# ---------------------------------------------------------------------------
# get_chain_for_role
# ---------------------------------------------------------------------------

class TestGetChainForRole:
    def _chain(self, role, custom=None):
        from local_model_router.helpers.smart_router.failover import get_chain_for_role
        return get_chain_for_role(role, custom)

    def test_default_chat_chain(self):
        chain = self._chain("chat")
        assert chain[0] == "chat"
        assert "utility" in chain

    def test_default_embed_chain_has_no_fallback(self):
        chain = self._chain("embed")
        assert chain == ["embed"]

    def test_custom_chain_overrides_default(self):
        custom = {"chat": ["slot_a", "slot_b"]}
        assert self._chain("chat", custom) == ["slot_a", "slot_b"]

    def test_unknown_role_returns_role_as_singleton(self):
        chain = self._chain("custom_role")
        assert chain == ["custom_role"]


# ---------------------------------------------------------------------------
# CooldownTracker
# ---------------------------------------------------------------------------

class TestCooldownTracker:
    def _tracker(self):
        from local_model_router.helpers.smart_router.failover import (
            CooldownTracker, CooldownProbe,
        )
        return CooldownTracker(), CooldownProbe(interval_seconds=30, max_attempts=3)

    def test_mark_error_makes_slot_appear_in_error_list(self):
        tracker, _ = self._tracker()
        tracker.mark_error("slot_chat", "connection refused")
        assert "slot_chat" in tracker.get_error_slots()

    def test_mark_error_idempotent(self):
        tracker, _ = self._tracker()
        tracker.mark_error("slot_chat", "first error")
        tracker.mark_error("slot_chat", "second error")
        assert tracker.get_error_slots().count("slot_chat") == 1

    def test_mark_recovered_removes_slot(self):
        tracker, _ = self._tracker()
        tracker.mark_error("slot_chat")
        tracker.mark_recovered("slot_chat")
        assert "slot_chat" not in tracker.get_error_slots()

    def test_should_probe_true_immediately_after_mark_error(self):
        tracker, config = self._tracker()
        tracker.mark_error("slot_chat")
        # probe_count=0, elapsed >= 0 * interval → should probe
        assert tracker.should_probe("slot_chat", config) is True

    def test_should_probe_false_after_max_attempts(self):
        tracker, config = self._tracker()
        tracker.mark_error("slot_chat")
        for _ in range(config.max_attempts):
            tracker.record_probe("slot_chat")
        assert tracker.should_probe("slot_chat", config) is False

    def test_should_probe_false_for_unknown_slot(self):
        tracker, config = self._tracker()
        assert tracker.should_probe("nonexistent_slot", config) is False

    def test_record_probe_increments_count(self):
        tracker, config = self._tracker()
        tracker.mark_error("slot_chat")
        tracker.record_probe("slot_chat")
        status = tracker.get_status("slot_chat")
        assert status["probe_count"] == 1


# ---------------------------------------------------------------------------
# should_recover (time-based)
# ---------------------------------------------------------------------------

class TestShouldRecover:
    def test_false_when_no_failure_recorded(self):
        from local_model_router.helpers.smart_router.failover import (
            SlotFailoverState, should_recover,
        )
        state = SlotFailoverState(chain=["chat", "utility"])
        assert should_recover(state) is False

    def test_false_when_within_recovery_window(self):
        from local_model_router.helpers.smart_router.failover import (
            SlotFailoverState, should_recover,
        )
        state = SlotFailoverState(chain=["chat", "utility"], recovery_seconds=300)
        state.failed_at = time.monotonic()
        assert should_recover(state) is False

    def test_true_when_recovery_window_elapsed(self):
        from local_model_router.helpers.smart_router.failover import (
            SlotFailoverState, should_recover,
        )
        state = SlotFailoverState(chain=["chat", "utility"], recovery_seconds=1)
        state.failed_at = time.monotonic() - 2  # 2 seconds ago > 1s window
        assert should_recover(state) is True

"""
Tests for BackendManager.select_slot_with_failover_async().

Uses an injected async health checker so no real network calls are made.
Pattern mirrors test_select_slot_with_failover.py but covers the async path.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_MINIMAL_CONFIG = """\
active_slots:
  - id: chat
    port: 8080
    host: localhost
    enabled: true
  - id: utility
    port: 8088
    host: localhost
    enabled: true
global:
  backend: remote
"""

_CUSTOM_CHAIN_CONFIG = """\
active_slots:
  - id: chat
    port: 8080
    host: localhost
    enabled: true
  - id: utility
    port: 8088
    host: localhost
    enabled: true
global:
  backend: remote
  failover_chains:
    chat:
      - utility
      - chat
"""


def _make_manager(tmp_path, yaml_content=_MINIMAL_CONFIG):
    from local_model_router.helpers.llama_cpp_manager import BackendManager
    BackendManager._instance = None
    cfg = tmp_path / "llama_cpp_servers.yaml"
    cfg.write_text(yaml_content)
    return BackendManager(str(cfg))


class _AsyncStubChecker:
    """Replaces SlotHealthChecker; maps port → health string for both sync and async."""

    def __init__(self, results_by_port: dict):
        self._results = results_by_port

    def check(self, config):
        return self._results.get(config.get("port"), "unhealthy")

    async def check_async(self, config):
        return self._results.get(config.get("port"), "unhealthy")


# ---------------------------------------------------------------------------
# Happy path: primary slot healthy
# ---------------------------------------------------------------------------

def test_async_primary_healthy_returns_primary(tmp_path):
    manager = _make_manager(tmp_path)
    manager._health_checker = _AsyncStubChecker({8080: "healthy", 8088: "healthy"})

    result = asyncio.run(manager.select_slot_with_failover_async("chat"))

    assert result is not None
    assert result["slot_id"] == "chat"
    assert "localhost:8080" in result["url"]
    assert result["is_failover"] is False


# ---------------------------------------------------------------------------
# Failover: primary unhealthy, secondary healthy
# ---------------------------------------------------------------------------

def test_async_primary_unhealthy_uses_secondary(tmp_path):
    manager = _make_manager(tmp_path)
    manager._health_checker = _AsyncStubChecker({8080: "unhealthy", 8088: "healthy"})

    result = asyncio.run(manager.select_slot_with_failover_async("chat"))

    assert result is not None
    assert result["slot_id"] == "utility"
    assert "localhost:8088" in result["url"]


# ---------------------------------------------------------------------------
# Chain exhausted
# ---------------------------------------------------------------------------

def test_async_chain_exhausted_returns_none(tmp_path):
    manager = _make_manager(tmp_path)
    manager._health_checker = _AsyncStubChecker({8080: "unhealthy", 8088: "unhealthy"})

    result = asyncio.run(manager.select_slot_with_failover_async("chat"))
    assert result is None


# ---------------------------------------------------------------------------
# Cooldown tracker integration
# ---------------------------------------------------------------------------

def test_async_cooldown_slot_treated_as_unhealthy(tmp_path):
    manager = _make_manager(tmp_path)
    manager._health_checker = _AsyncStubChecker({8080: "healthy", 8088: "healthy"})
    manager._cooldown_tracker.mark_error("chat", "forced error")

    result = asyncio.run(manager.select_slot_with_failover_async("chat"))

    # chat is in cooldown → async path skips probe, falls to utility
    assert result is not None
    assert result["slot_id"] == "utility"


def test_async_all_in_cooldown_returns_none(tmp_path):
    manager = _make_manager(tmp_path)
    manager._health_checker = _AsyncStubChecker({8080: "healthy", 8088: "healthy"})
    manager._cooldown_tracker.mark_error("chat")
    manager._cooldown_tracker.mark_error("utility")

    result = asyncio.run(manager.select_slot_with_failover_async("chat"))
    assert result is None


# ---------------------------------------------------------------------------
# preferred_slot
# ---------------------------------------------------------------------------

def test_async_preferred_slot_starts_from_that_slot(tmp_path):
    manager = _make_manager(tmp_path)
    manager._health_checker = _AsyncStubChecker({8080: "healthy", 8088: "healthy"})

    result = asyncio.run(
        manager.select_slot_with_failover_async("chat", preferred_slot="utility")
    )

    assert result is not None
    assert result["slot_id"] == "utility"


def test_async_preferred_slot_unhealthy_falls_back(tmp_path):
    """preferred_slot='utility' is unhealthy; DEFAULT_CHAINS["utility"] walks to chat."""
    manager = _make_manager(tmp_path)
    manager._health_checker = _AsyncStubChecker({8080: "healthy", 8088: "unhealthy"})

    result = asyncio.run(
        manager.select_slot_with_failover_async("utility", preferred_slot="utility")
    )

    assert result is not None
    assert result["slot_id"] == "chat"


# ---------------------------------------------------------------------------
# Custom failover chains from config
# ---------------------------------------------------------------------------

def test_async_custom_chain_overrides_default(tmp_path):
    """Config sets chat chain = [utility, chat]; utility should be tried first."""
    manager = _make_manager(tmp_path, _CUSTOM_CHAIN_CONFIG)
    # Only utility is healthy
    manager._health_checker = _AsyncStubChecker({8080: "unhealthy", 8088: "healthy"})

    result = asyncio.run(manager.select_slot_with_failover_async("chat"))

    assert result is not None
    assert result["slot_id"] == "utility"


# ---------------------------------------------------------------------------
# Explicit chain (ranked-candidate order) overrides config chains
# ---------------------------------------------------------------------------

def test_async_explicit_chain_overrides_config_chain(tmp_path):
    """Config puts utility first for chat, but the explicit chain wins."""
    manager = _make_manager(tmp_path, _CUSTOM_CHAIN_CONFIG)
    manager._health_checker = _AsyncStubChecker({8080: "healthy", 8088: "healthy"})

    result = asyncio.run(
        manager.select_slot_with_failover_async("chat", chain=["chat", "utility"])
    )

    assert result is not None
    assert result["slot_id"] == "chat"
    assert result["fallback_chain"] == ["chat", "utility"]


def test_async_explicit_chain_walked_in_order(tmp_path):
    manager = _make_manager(tmp_path)
    manager._health_checker = _AsyncStubChecker({8080: "unhealthy", 8088: "healthy"})

    result = asyncio.run(
        manager.select_slot_with_failover_async("chat", chain=["chat", "utility"])
    )

    assert result is not None
    assert result["slot_id"] == "utility"
    assert result["is_failover"] is True
    assert result["failover_reason"]


def test_async_empty_chain_falls_back_to_config(tmp_path):
    manager = _make_manager(tmp_path)
    manager._health_checker = _AsyncStubChecker({8080: "healthy", 8088: "healthy"})

    result = asyncio.run(manager.select_slot_with_failover_async("chat", chain=[]))

    assert result is not None
    assert result["slot_id"] == "chat"  # DEFAULT_CHAINS["chat"] starts at chat


def test_sync_explicit_chain_walked_in_order(tmp_path):
    manager = _make_manager(tmp_path)
    manager._health_checker = _AsyncStubChecker({8080: "unhealthy", 8088: "healthy"})

    result = manager.select_slot_with_failover("chat", chain=["chat", "utility"])

    assert result is not None
    assert result["slot_id"] == "utility"
    assert result["is_failover"] is True


# ---------------------------------------------------------------------------
# No slots configured
# ---------------------------------------------------------------------------

def test_async_no_slots_configured_returns_none(tmp_path):
    from local_model_router.helpers.llama_cpp_manager import BackendManager
    BackendManager._instance = None
    cfg = tmp_path / "llama_cpp_servers.yaml"
    cfg.write_text("active_slots: []\nglobal:\n  backend: remote\n")
    manager = BackendManager(str(cfg))
    manager._health_checker = _AsyncStubChecker({})

    result = asyncio.run(manager.select_slot_with_failover_async("chat"))
    assert result is None


# ---------------------------------------------------------------------------
# Health probe called once per slot, not per chain traversal step
# ---------------------------------------------------------------------------

def test_async_health_probe_called_once_per_slot(tmp_path):
    """Verify each slot is probed exactly once per routing decision."""
    manager = _make_manager(tmp_path)
    probe_counts = {}

    class CountingChecker:
        async def check_async(self, config):
            port = config.get("port")
            probe_counts[port] = probe_counts.get(port, 0) + 1
            return "unhealthy"

        def check(self, config):
            return "unhealthy"

    manager._health_checker = CountingChecker()
    asyncio.run(manager.select_slot_with_failover_async("chat"))

    # Each slot in the chain should be probed at most once
    for port, count in probe_counts.items():
        assert count == 1, f"slot port {port} was probed {count} times"


# ---------------------------------------------------------------------------
# Sync path still works identically after async methods were added
# ---------------------------------------------------------------------------

def test_sync_path_unaffected_by_async_addition(tmp_path, monkeypatch):
    """Existing sync select_slot_with_failover() still works correctly."""
    manager = _make_manager(tmp_path)
    monkeypatch.setattr(manager, "_get_slot_health", lambda slot_id: "healthy")

    result = manager.select_slot_with_failover("chat")

    assert result is not None
    assert result["slot_id"] == "chat"
    assert "localhost:8080" in result["url"]


# ---------------------------------------------------------------------------
# router_bridge.chat_complete: routing decision computed once (no double-call)
# ---------------------------------------------------------------------------

"""Process-local slot locking, request draining and singleton config identity."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from local_model_router.helpers.llama_cpp_manager import BackendManager
from local_model_router.helpers.backends.base import SlotStatus


def _manager() -> BackendManager:
    manager = object.__new__(BackendManager)
    manager._slot_configs = {"slot_chat": {"port": 8080}}
    return manager


def test_slot_operation_closes_ingress_and_epoch_rejects_queued_route():
    manager = _manager()
    epoch = manager.slot_epoch("slot_chat")
    assert manager.try_begin_slot_request("slot_chat", expected_epoch=epoch) == "ok"
    assert manager.begin_slot_operation("slot_chat", "draining") is True
    assert manager.try_begin_slot_request("slot_chat", expected_epoch=epoch) == "gpu_swap_in_progress"

    manager.finish_slot_request("slot_chat")
    manager.finish_slot_operation("slot_chat")

    assert manager.try_begin_slot_request("slot_chat", expected_epoch=epoch) == "gpu_model_changed"
    assert manager.try_begin_slot_request("slot_chat", expected_epoch=manager.slot_epoch("slot_chat")) == "ok"
    manager.finish_slot_request("slot_chat")


def test_drain_waits_past_notice_without_cancelling_request():
    manager = _manager()
    assert manager.try_begin_slot_request("slot_chat") == "ok"
    assert manager.begin_slot_operation("slot_chat", "draining") is True
    notices: list[str] = []

    async def scenario():
        waiter = asyncio.create_task(
            manager.wait_for_slot_idle(
                "slot_chat",
                notice_seconds=0.01,
                on_wait=lambda: notices.append("waiting"),
            )
        )
        await asyncio.sleep(0.08)
        assert waiter.done() is False
        assert manager.slot_operation_snapshot("slot_chat")["active_requests"] == 1
        assert manager.slot_operation_snapshot("slot_chat")["phase"] == "waiting_for_requests"
        manager.finish_slot_request("slot_chat")
        await asyncio.wait_for(waiter, timeout=1)

    asyncio.run(scenario())
    assert notices == ["waiting"]
    manager.finish_slot_operation("slot_chat")


def test_manual_stop_blocks_auto_restart_until_explicit_start():
    manager = _manager()
    manager.set_manual_stop("slot_chat", True)
    assert manager.slot_operation_snapshot("slot_chat")["manual_stop"] is True
    manager.set_manual_stop("slot_chat", False)
    assert manager.slot_operation_snapshot("slot_chat")["manual_stop"] is False


def test_singleton_accepts_normalized_same_path_and_rejects_another(tmp_path, monkeypatch):
    first = tmp_path / "first.yaml"
    first.write_text("active_slots: []\nglobal:\n  backend: remote\n", encoding="utf-8")
    second = tmp_path / "second.yaml"
    second.write_text("active_slots: []\nglobal:\n  backend: remote\n", encoding="utf-8")
    previous = BackendManager._instance
    BackendManager._instance = None
    try:
        manager = BackendManager.get_instance(str(first))
        same = Path(first.parent, ".", first.name)
        assert BackendManager.get_instance(str(same)) is manager
        with pytest.raises(ValueError, match="different config path"):
            BackendManager.get_instance(str(second))
        monkeypatch.setattr(
            "local_model_router.helpers.llama_cpp_manager._default_config_path",
            lambda: str(second),
        )
        with pytest.raises(ValueError, match="different config path"):
            BackendManager.get_instance()
    finally:
        BackendManager._instance = previous


def test_auto_restart_skips_swap_and_manual_stop():
    class Backend:
        def __init__(self):
            self.starts = 0

        async def list_slots(self):
            return {"slot_chat": SlotStatus(name="slot_chat", running=False)}

        async def health_check(self, _name):
            return SlotStatus(name="slot_chat", running=False, healthy=False)

        async def start_slot(self, _name, _config):
            self.starts += 1
            return SlotStatus(name="slot_chat", running=True, healthy=True)

    manager = _manager()
    backend = Backend()
    manager._backend = backend
    manager.global_config = {"max_restart_attempts": 3}
    manager._restart_attempts = {}
    manager.logger = logging.getLogger("test.slot_coordination")

    assert manager.begin_slot_operation("slot_chat", "loading") is True
    asyncio.run(manager._restart_unhealthy_slots())
    manager.finish_slot_operation("slot_chat")
    assert backend.starts == 0

    manager.set_manual_stop("slot_chat", True)
    asyncio.run(manager._restart_unhealthy_slots())
    assert backend.starts == 0

    manager.set_manual_stop("slot_chat", False)
    manager.set_auto_restart_suppressed("slot_chat", True)
    asyncio.run(manager._restart_unhealthy_slots())
    assert backend.starts == 0

    manager.set_auto_restart_suppressed("slot_chat", False)
    asyncio.run(manager._restart_unhealthy_slots())
    assert backend.starts == 1


def test_stop_all_rejects_unowned_occupied_port(tmp_path, monkeypatch):
    class Backend:
        async def stop_slot(self, _name):
            return False

    manager = _manager()
    manager._backend = Backend()
    manager._restart_task = None
    monkeypatch.setattr(
        "local_model_router.helpers.llama_cpp_manager._tcp_port_open",
        lambda _host, _port: True,
    )

    result = asyncio.run(manager.stop_all())

    assert result == {"slot_chat": False}
    assert manager.slot_operation_snapshot("slot_chat")["manual_stop"] is True


def test_stop_all_does_not_mark_manual_stop_for_competing_operation(monkeypatch):
    class Backend:
        async def stop_slot(self, _name):
            raise AssertionError("busy operation must own the slot")

    manager = _manager()
    manager._backend = Backend()
    manager._restart_task = None
    monkeypatch.setattr(
        "local_model_router.helpers.llama_cpp_manager._tcp_port_open",
        lambda _host, _port: False,
    )
    assert manager.begin_slot_operation("slot_chat", "loading") is True

    result = asyncio.run(manager.stop_all())

    assert result == {"slot_chat": False}
    assert manager.slot_operation_snapshot("slot_chat")["manual_stop"] is False
    manager.finish_slot_operation("slot_chat")


def test_stop_slot_waits_for_active_request_and_then_stops():
    class Backend:
        def __init__(self):
            self.stops = 0

        async def stop_slot(self, _name):
            self.stops += 1
            return True

    manager = _manager()
    backend = Backend()
    manager._backend = backend
    assert manager.try_begin_slot_request("slot_chat") == "ok"

    async def scenario():
        stopping = asyncio.create_task(manager.stop_slot("slot_chat"))
        await asyncio.sleep(0.08)
        assert stopping.done() is False
        assert backend.stops == 0, "the in-flight request must not be cut off"
        manager.finish_slot_request("slot_chat")
        return await asyncio.wait_for(stopping, timeout=1)

    assert asyncio.run(scenario()) is True
    assert backend.stops == 1
    assert manager.last_slot_failure_code("slot_chat") == ""
    assert manager.slot_operation_snapshot("slot_chat")["manual_stop"] is True
    assert manager.slot_operation_snapshot("slot_chat")["phase"] == "idle"


def test_auto_restart_waits_for_active_request_before_starting():
    class Backend:
        def __init__(self):
            self.starts = 0

        async def list_slots(self):
            return {"slot_chat": SlotStatus(name="slot_chat", running=False)}

        async def health_check(self, _name):
            return SlotStatus(name="slot_chat", running=False, healthy=False)

        async def start_slot(self, _name, _config):
            self.starts += 1
            return SlotStatus(name="slot_chat", running=True, healthy=True)

    manager = _manager()
    backend = Backend()
    manager._backend = backend
    manager.global_config = {"max_restart_attempts": 3}
    manager._restart_attempts = {}
    manager.logger = logging.getLogger("test.slot_coordination")

    assert manager.try_begin_slot_request("slot_chat") == "ok"

    async def scenario():
        restarting = asyncio.create_task(manager._restart_unhealthy_slots())
        await asyncio.sleep(0.08)
        assert restarting.done() is False
        assert backend.starts == 0
        assert manager._restart_attempts.get("slot_chat", 0) == 0
        manager.finish_slot_request("slot_chat")
        await asyncio.wait_for(restarting, timeout=1)

    asyncio.run(scenario())

    assert backend.starts == 1
    assert manager._restart_attempts["slot_chat"] == 1
    assert manager.slot_operation_snapshot("slot_chat")["phase"] == "idle"


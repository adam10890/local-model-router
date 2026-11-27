"""Studio lease hold: a held Hermes request resumes the moment the lease ends."""

import asyncio

from test_gpu_swap_api import _app


def _lease(manager, on):
    manager.set_manual_stop("slot_chat", on)
    manager.set_lease_reason("slot_chat", "studio_lease" if on else None)


def test_lease_hold_returns_when_lease_ends(tmp_path, monkeypatch):
    _app_obj, manager, watcher = _app(tmp_path, monkeypatch)

    async def scenario():
        _lease(manager, True)

        async def release_soon():
            await asyncio.sleep(1.5)
            _lease(manager, False)

        task = asyncio.create_task(release_soon())
        retry, waited = await watcher.wait_while_leased(max_wait_seconds=30)
        await task
        assert retry is None or retry[0] != "gpu_leased_by_studio"
        assert 1000 <= waited < 5000  # resumed ~1s after release, not after a fixed delay

    asyncio.run(scenario())


def test_lease_hold_disabled_or_expired_keeps_503(tmp_path, monkeypatch):
    _app_obj, manager, watcher = _app(tmp_path, monkeypatch)

    async def scenario():
        _lease(manager, True)
        retry, waited = await watcher.wait_while_leased()  # config default 0 = old behavior
        assert retry[0] == "gpu_leased_by_studio" and waited == 0
        retry, waited = await watcher.wait_while_leased(max_wait_seconds=1)
        assert retry[0] == "gpu_leased_by_studio" and waited >= 1000
        _lease(manager, False)

    asyncio.run(scenario())

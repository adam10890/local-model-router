"""Shared aiohttp client pool with monkeypatch compatibility.

The router shares one long-lived ``aiohttp.ClientSession`` (backed by a
``TCPConnector``) per running event loop for upstream forwarding, slot model
discovery, and async health probes. When tests monkeypatch
``aiohttp.ClientSession`` with a fake factory or lambda, calls fall back cleanly
to short-lived sessions from the patched callable.
"""
from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional, Tuple

import aiohttp

# Captured before any test fixture monkeypatches aiohttp.ClientSession so
# SharedHttpClient can detect active test doubles and fall back per call.
_REAL_CLIENT_SESSION = aiohttp.ClientSession

_DEFAULT_CONNECTOR_LIMIT = 64
_DEFAULT_DNS_CACHE_TTL = 300


async def _close_session_object(session: Any) -> None:
    """Close a real or mocked ClientSession, awaiting async close() if needed."""
    close = getattr(session, "close", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


class SharedHttpClient:
    """Own and reuse a pooled ``aiohttp.ClientSession`` for a service lifetime."""

    def __init__(
        self,
        *,
        connector_limit: int = _DEFAULT_CONNECTOR_LIMIT,
        ttl_dns_cache: int = _DEFAULT_DNS_CACHE_TTL,
    ) -> None:
        self._connector_limit = connector_limit
        self._ttl_dns_cache = ttl_dns_cache
        self._session: Optional[aiohttp.ClientSession] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @staticmethod
    def is_patched() -> bool:
        """Return True when tests have replaced ``aiohttp.ClientSession``."""
        return aiohttp.ClientSession is not _REAL_CLIENT_SESSION

    @staticmethod
    def _new_patched_session(timeout: Optional[aiohttp.ClientTimeout] = None) -> Any:
        if timeout is not None:
            try:
                return aiohttp.ClientSession(timeout=timeout)
            except TypeError:
                pass
        return aiohttp.ClientSession()

    def _get_or_create_shared_session(self) -> aiohttp.ClientSession:
        loop = asyncio.get_running_loop()
        if (
            self._session is not None
            and self._loop is loop
            and not getattr(self._session, "closed", False)
        ):
            return self._session

        # A loop switch happens across repeated TestClient runs; drop the stale
        # reference cleanly rather than awaiting across a closed/foreign loop.
        connector = aiohttp.TCPConnector(
            limit=self._connector_limit,
            ttl_dns_cache=self._ttl_dns_cache,
        )
        session = aiohttp.ClientSession(connector=connector)
        self._session = session
        self._loop = loop
        return session

    @asynccontextmanager
    async def request_session(
        self,
        *,
        timeout: Optional[aiohttp.ClientTimeout] = None,
    ) -> AsyncIterator[Any]:
        """Yield a shared real session or a scoped patched test session."""
        if self.is_patched():
            session = self._new_patched_session(timeout=timeout)
            if hasattr(session, "__aenter__") and hasattr(session, "__aexit__"):
                async with session as entered:
                    yield entered
                return
            try:
                yield session
            finally:
                await _close_session_object(session)
            return

        yield self._get_or_create_shared_session()

    async def open_stream_session(
        self,
        *,
        timeout: Optional[aiohttp.ClientTimeout] = None,
    ) -> Tuple[Any, bool]:
        """Return ``(session, should_close)`` for a long-lived SSE stream."""
        if self.is_patched():
            return self._new_patched_session(timeout=timeout), True
        return self._get_or_create_shared_session(), False

    async def close(self) -> None:
        """Close the pooled session cleanly during service shutdown."""
        session = self._session
        loop = self._loop
        self._session = None
        self._loop = None
        if session is None or getattr(session, "closed", False):
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and loop is running and not running.is_closed():
            await _close_session_object(session)

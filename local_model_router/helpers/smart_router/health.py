"""
Slot health probing, isolated from BackendManager routing logic.

SlotHealthChecker accepts injectable probe_fn / async_probe_fn so callers
(tests, future adapters) can replace the network layer without touching
routing logic.

Sync default:  _urllib_probe   (stdlib only, used by _get_slot_health)
Async default: _aiohttp_probe  (aiohttp, used by _get_slot_health_async)

Both return {"ok": bool, ...} and are safe to swap independently.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("lmm_router.health")

HEALTHY = "healthy"
UNHEALTHY = "unhealthy"
UNKNOWN = "unknown"

DEFAULT_CACHE_TTL = 3.0
CACHE_TTL_ENV = "A0_LMM_ROUTER_HEALTH_CACHE_TTL"


def _resolve_cache_ttl(cache_ttl: Optional[float]) -> float:
    """Resolve the probe-result TTL: explicit arg > env > default. <=0 disables."""
    if cache_ttl is None:
        raw = os.environ.get(CACHE_TTL_ENV, "")
        try:
            cache_ttl = float(raw) if raw.strip() else DEFAULT_CACHE_TTL
        except ValueError:
            cache_ttl = DEFAULT_CACHE_TTL
    return max(0.0, float(cache_ttl))


def _urllib_probe(url: str, timeout: int) -> Dict:
    """Default sync probe: GET /health, return {"ok": bool}.

    Isolated here so it can be swapped without touching SlotHealthChecker.
    """
    import urllib.request  # noqa: PLC0415

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            if resp.status == 200:
                data = json.loads(resp.read().decode())
                return {"ok": data.get("status") == "ok", "http_status": resp.status}
            return {"ok": False, "http_status": resp.status}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def _aiohttp_probe(url: str, timeout: int) -> Dict:
    """Default async probe: GET /health via aiohttp, return {"ok": bool}."""
    import aiohttp  # noqa: PLC0415

    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    return {"ok": data.get("status") == "ok", "http_status": resp.status}
                return {"ok": False, "http_status": resp.status}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


class SlotHealthChecker:
    """Probe a single slot's /health endpoint and return a health string.

    Parameters
    ----------
    timeout:
        Seconds to wait for a probe response (applied to both sync and async paths).
    probe_fn:
        Sync callable(url: str, timeout: int) -> {"ok": bool, ...}.
        Defaults to the stdlib urllib probe.  Inject a stub in tests.
    async_probe_fn:
        Async callable(url: str, timeout: int) -> {"ok": bool, ...}.
        Defaults to the aiohttp probe.  Inject an async stub in tests.
    cache_ttl:
        Seconds a probe result stays fresh before the slot is probed again.
        Shared between the sync and async paths, keyed by probe URL.
        None → A0_LMM_ROUTER_HEALTH_CACHE_TTL env, else 3.0. <=0 disables
        caching (every check probes), preserving pre-cache behavior.
    """

    def __init__(
        self,
        timeout: int = 2,
        probe_fn: Optional[Callable[[str, int], Dict]] = None,
        async_probe_fn: Optional[Any] = None,
        cache_ttl: Optional[float] = None,
    ) -> None:
        self.timeout = timeout
        self._probe = probe_fn or _urllib_probe
        self._async_probe = async_probe_fn  # None → resolved at call time
        self.cache_ttl = _resolve_cache_ttl(cache_ttl)
        self._cache: Dict[str, Tuple[float, str]] = {}  # url -> (expires_at, status)

    def _cached_status(self, url: str) -> Optional[str]:
        if self.cache_ttl <= 0:
            return None
        entry = self._cache.get(url)
        if entry is None:
            return None
        expires_at, status = entry
        if time.monotonic() >= expires_at:
            self._cache.pop(url, None)
            return None
        return status

    def _store_status(self, url: str, status: str) -> str:
        if self.cache_ttl > 0:
            self._cache[url] = (time.monotonic() + self.cache_ttl, status)
        return status

    def invalidate(self, url: Optional[str] = None) -> None:
        """Drop cached probe results (all of them, or one URL's)."""
        if url is None:
            self._cache.clear()
        else:
            self._cache.pop(url, None)

    def check(self, slot_config: Dict) -> str:
        """Return HEALTHY, UNHEALTHY, or UNKNOWN for the given slot config.

        slot_config must contain at least 'host' and 'port' keys.
        Missing / falsy port → UNKNOWN (cannot construct a valid URL).
        """
        host = slot_config.get("host", "localhost")
        port = slot_config.get("port")
        if not port:
            return UNKNOWN

        url = f"http://{host}:{port}/health"
        cached = self._cached_status(url)
        if cached is not None:
            return cached
        try:
            result = self._probe(url, self.timeout)
            return self._store_status(url, HEALTHY if result.get("ok") else UNHEALTHY)
        except Exception:
            logger.debug("Health probe raised unexpectedly for %s:%s", host, port)
            return self._store_status(url, UNHEALTHY)

    async def check_async(self, slot_config: Dict) -> str:
        """Async version of check(). Does not block the event loop.

        Uses _aiohttp_probe by default; override with async_probe_fn for tests.
        Return values are identical to check(): HEALTHY, UNHEALTHY, or UNKNOWN.
        """
        host = slot_config.get("host", "localhost")
        port = slot_config.get("port")
        if not port:
            return UNKNOWN

        url = f"http://{host}:{port}/health"
        cached = self._cached_status(url)
        if cached is not None:
            return cached
        probe = self._async_probe or _aiohttp_probe
        try:
            result = await probe(url, self.timeout)
            return self._store_status(url, HEALTHY if result.get("ok") else UNHEALTHY)
        except Exception:
            logger.debug("Async health probe raised unexpectedly for %s:%s", host, port)
            return self._store_status(url, UNHEALTHY)

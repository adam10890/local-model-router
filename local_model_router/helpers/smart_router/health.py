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
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("lmm_router.health")

HEALTHY = "healthy"
UNHEALTHY = "unhealthy"
UNKNOWN = "unknown"


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
    """

    def __init__(
        self,
        timeout: int = 2,
        probe_fn: Optional[Callable[[str, int], Dict]] = None,
        async_probe_fn: Optional[Any] = None,
    ) -> None:
        self.timeout = timeout
        self._probe = probe_fn or _urllib_probe
        self._async_probe = async_probe_fn  # None → resolved at call time

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
        try:
            result = self._probe(url, self.timeout)
            return HEALTHY if result.get("ok") else UNHEALTHY
        except Exception:
            logger.debug("Health probe raised unexpectedly for %s:%s", host, port)
            return UNHEALTHY

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
        probe = self._async_probe or _aiohttp_probe
        try:
            result = await probe(url, self.timeout)
            return HEALTHY if result.get("ok") else UNHEALTHY
        except Exception:
            logger.debug("Async health probe raised unexpectedly for %s:%s", host, port)
            return UNHEALTHY

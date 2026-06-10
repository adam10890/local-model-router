"""Aggregate ``GET /v1/models`` from fleet slots plus the router's aliases.

The listing has two kinds of entries:

- ``alias`` — the router's stable model surface (``auto``, ``fast``,
  ``coder``, …). Clients should prefer these; they survive fleet changes.
- ``slot_model`` — whatever each live slot actually reports on its own
  ``/v1/models``, enriched with slot id, role, and context size when the
  upstream provides it (llama.cpp Router Mode reports ``meta.n_ctx``).

The fetch function is injectable so tests stay hermetic.
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

import aiohttp

from local_model_router.routing.aliases import public_aliases

FetchFn = Callable[[str], Awaitable[Optional[List[Dict[str, Any]]]]]

_FETCH_TIMEOUT_SECONDS = 3.0


async def _default_fetch(base_url: str) -> Optional[List[Dict[str, Any]]]:
    """GET ``{base_url}/models`` and return its ``data`` list, or None."""
    url = base_url.rstrip("/") + "/models"
    try:
        timeout = aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                payload = await resp.json(content_type=None)
    except Exception:
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, list) else None


async def list_models(observer: Any, fetch: Optional[FetchFn] = None) -> Dict[str, Any]:
    """Build an OpenAI-compatible model list for the router."""
    fetch_fn = fetch or _default_fetch
    created = int(time.time())
    entries: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}

    for alias, role in sorted(public_aliases().items()):
        entry = {
            "id": alias,
            "object": "model",
            "created": created,
            "owned_by": "local-model-router",
            "meta": {"kind": "alias", "maps_to_role": role},
        }
        entries.append(entry)
        by_id[alias] = entry

    for slot in observer.get_slots():
        if not slot.get("enabled") or not slot.get("base_url"):
            continue
        rows = await fetch_fn(slot["base_url"]) or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            model_id = str(row.get("id") or "").strip()
            if not model_id:
                continue
            row_meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
            live = {
                "slot_id": slot.get("id"),
                "role": slot.get("role"),
                "base_url": slot.get("base_url"),
                "n_ctx": row_meta.get("n_ctx"),
            }
            existing = by_id.get(model_id)
            if existing is not None:
                # a live slot serves a model named like one of our aliases —
                # keep the alias entry and attach the live serving info
                existing["meta"].setdefault("live", live)
                continue
            entry = {
                "id": model_id,
                "object": "model",
                "created": row.get("created") or created,
                "owned_by": "local-fleet",
                "meta": {"kind": "slot_model", **live},
            }
            entries.append(entry)
            by_id[model_id] = entry

    return {"object": "list", "data": entries}

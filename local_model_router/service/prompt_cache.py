"""Small opt-in prompt cache for deterministic local requests."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from collections import OrderedDict
from typing import Any, Optional

_ENABLE_ENV = "A0_LMM_ROUTER_PROMPT_CACHE"
_TTL_ENV = "A0_LMM_ROUTER_PROMPT_CACHE_TTL"
_MAX_ENV = "A0_LMM_ROUTER_PROMPT_CACHE_MAX"


def prompt_cache_enabled() -> bool:
    return os.environ.get(_ENABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def is_cacheable(body: dict[str, Any]) -> bool:
    if body.get("stream") is True:
        return False
    temperature = body.get("temperature", 0.0)
    try:
        temp = float(temperature or 0.0)
    except (TypeError, ValueError):
        temp = 0.0
    return temp == 0.0 or body.get("seed") is not None


def cache_key(body: dict[str, Any], *, resolved_model: str) -> str:
    payload = {
        "model": resolved_model,
        "messages": body.get("messages") or [],
        "temperature": body.get("temperature", 0.0),
        "tools": body.get("tools") or None,
        "tool_choice": body.get("tool_choice"),
        "response_format": body.get("response_format"),
        "seed": body.get("seed"),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _int_env(name: str, default: int, minimum: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


class InMemoryPromptCache:
    def __init__(self, *, ttl_seconds: Optional[int] = None, max_size: Optional[int] = None) -> None:
        self.ttl_seconds = ttl_seconds if ttl_seconds is not None else _int_env(_TTL_ENV, 3600, 1)
        self.max_size = max_size if max_size is not None else _int_env(_MAX_ENV, 1000, 1)
        self._store: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

    def get(self, key: str) -> Optional[dict[str, Any]]:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.time() >= expires_at:
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        return copy.deepcopy(value)

    def set(self, key: str, value: dict[str, Any]) -> None:
        self._store[key] = (time.time() + self.ttl_seconds, copy.deepcopy(value))
        self._store.move_to_end(key)
        while len(self._store) > self.max_size:
            self._store.popitem(last=False)


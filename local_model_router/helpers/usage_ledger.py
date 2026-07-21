"""Local usage ledger for declared-limit providers.

Records token/request usage for upstream providers whose usage is neither
available from a live API nor proxied through Imperium itself (e.g. Ollama
Cloud, custom subscription providers). A later phase adds a live reader for
Codex; this ledger is the fallback source for the rest of the declared-limit
providers, counted locally.

Append-only JSONL, single file — Imperium records usage server-side in one
process (the Starlette HTTP service), so there is no need for the
per-process file split used by the sibling a0-plugin ledger this module is
adapted from.

Data-dir convention matches FleetStore._default_db_path()
(local_model_router/service/fleet_manager.py:81-83) and
agent_orchestrator._default_workspace_root()
(local_model_router/service/agent_orchestrator.py:60-61): a file/dir under
``Path(tempfile.gettempdir()) / "a0_lmm_router"``, overridable via an env var.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

logger = logging.getLogger("local_model_router.usage_ledger")

_lock = Lock()

LEDGER_PATH_ENV = "A0_USAGE_LEDGER_PATH"

# Overridable in tests (monkeypatch.setattr(usage_ledger, "LEDGER_PATH", ...)),
# same as stats_tracker._stats_path / fleet_manager's db_path.
LEDGER_PATH: Path = Path(
    os.environ.get(LEDGER_PATH_ENV, "").strip()
    or str(Path(tempfile.gettempdir()) / "a0_lmm_router" / "usage_ledger.jsonl")
)

MAX_AGE_DAYS = 35

# path -> ((mtime, size), events) — size in the key because Windows mtime is coarse.
_cache: Dict[str, tuple] = {}
_last_prune_day: Optional[str] = None


def record_usage(
    provider_id: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    requests: int = 1,
    model: str = "",
    source: str = "",
) -> None:
    """Append one usage event to the ledger."""
    event = {
        "ts": time.time(),
        "provider_id": provider_id,
        "model": model,
        "tokens_in": int(tokens_in),
        "tokens_out": int(tokens_out),
        "requests": int(requests),
        "source": source,
    }
    with _lock:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LEDGER_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()
    _maybe_prune()


def _load_file(path: Path) -> List[dict]:
    """Parse the ledger file, dropping events older than MAX_AGE_DAYS. Cached by (mtime, size)."""
    try:
        stat = path.stat()
    except OSError:
        return []
    key = (stat.st_mtime, stat.st_size)
    cached = _cache.get(str(path))
    if cached and cached[0] == key:
        return cached[1]

    cutoff = time.time() - MAX_AGE_DAYS * 86400
    events: List[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("ts", 0) >= cutoff:
                    events.append(ev)
    except OSError:
        return []
    _cache[str(path)] = (key, events)
    return events


def all_events(since_ts: float = 0.0) -> List[dict]:
    """Ledger events at or after since_ts, dropping anything older than MAX_AGE_DAYS."""
    return [e for e in _load_file(LEDGER_PATH) if e.get("ts", 0) >= since_ts]


def window_totals(provider_id: str, window_seconds: int, now: Optional[float] = None) -> Dict[str, int]:
    """Rolling-window sums for one provider: {"tokens": n, "requests": n}."""
    now = now if now is not None else time.time()
    tokens = requests = 0
    for ev in all_events(since_ts=now - window_seconds):
        if ev.get("provider_id") != provider_id or ev.get("ts", 0) > now:
            continue
        tokens += int(ev.get("tokens_in", 0)) + int(ev.get("tokens_out", 0))
        requests += int(ev.get("requests", 0))
    return {"tokens": tokens, "requests": requests}


def prune(max_age_days: int = MAX_AGE_DAYS) -> None:
    """Rewrite the ledger, dropping events older than max_age_days."""
    path = LEDGER_PATH
    if not path.exists():
        return
    cutoff = time.time() - max_age_days * 86400
    with _lock:
        keep: List[str] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get("ts", 0) >= cutoff:
                        keep.append(line)
                except ValueError:
                    continue
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            logger.warning("ledger prune failed: %s", e)


def _maybe_prune() -> None:
    """Opportunistic prune, at most once per process per day. Called outside _lock."""
    global _last_prune_day
    day = time.strftime("%Y-%m-%d")
    if day == _last_prune_day:
        return
    _last_prune_day = day
    try:
        prune()
    except Exception as e:  # accounting must never break the caller
        logger.warning("ledger prune failed: %s", e)

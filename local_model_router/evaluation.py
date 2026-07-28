"""Deterministic, one-shot local model evaluation."""
from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from local_model_router.service.fleet_manager import FleetStore

RequestFn = Callable[[str, str, Optional[dict[str, Any]]], Any]


def http_requester(base_url: str, api_key: str = "") -> RequestFn:
    base = base_url.rstrip("/")

    def request(method: str, path: str, payload: Optional[dict[str, Any]] = None) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120) as response:  # noqa: S310
                parsed = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{method} {path} failed: {exc}") from exc
        if not isinstance(parsed, (dict, list)):
            raise RuntimeError(f"{method} {path} did not return a JSON object or array")
        return parsed

    return request


def _message(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return {}
    message = choices[0].get("message")
    return message if isinstance(message, dict) else {}


def _content(data: dict[str, Any]) -> str:
    return str(_message(data).get("content") or "").strip()


def _valid_json(data: dict[str, Any]) -> bool:
    try:
        return json.loads(_content(data)) == {"ok": True}
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _valid_tool(data: dict[str, Any]) -> bool:
    calls = _message(data).get("tool_calls")
    if not isinstance(calls, list) or not calls or not isinstance(calls[0], dict):
        return False
    function = calls[0].get("function")
    if not isinstance(function, dict) or function.get("name") != "imperium_probe":
        return False
    try:
        arguments = function.get("arguments")
        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError:
        return False
    return parsed == {"value": 7}


def _valid_scribe(data: dict[str, Any]) -> bool:
    content = _content(data)
    return all(fact in content for fact in ("Orion", "17", "blue")) and len(content.split()) <= 30


def _valid_code(data: dict[str, Any]) -> bool:
    normalized = " ".join(_content(data).replace("\n", " ").split())
    return "def add(a, b):" in normalized and "return a + b" in normalized


def _valid_embedding(data: dict[str, Any]) -> bool:
    rows = data.get("data")
    if not isinstance(rows, list) or len(rows) < 2 or not all(isinstance(row, dict) for row in rows):
        return False
    vectors = [row.get("embedding") for row in rows]
    return all(
        isinstance(vector, list)
        and bool(vector)
        and len(vector) == len(vectors[0])
        and all(isinstance(value, (int, float)) and math.isfinite(value) for value in vector)
        for vector in vectors
    )


def _chat_payload(model_id: str, prompt: str, role: str) -> dict[str, Any]:
    return {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 128,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
        "routing": {"role": role, "local_only": True},
    }


def _cases(model_id: str, embedding_only: bool) -> list[tuple[str, str, str, dict[str, Any], Callable[[dict[str, Any]], bool]]]:
    if embedding_only:
        return [("embedding", "embed", "/v1/embeddings", {"model": model_id, "input": ["imperium probe", "imperium probe 2"]}, _valid_embedding)]
    tool_payload = _chat_payload(model_id, "Call imperium_probe with value 7.", "utility")
    tool_payload.update({
        "tools": [{
            "type": "function",
            "function": {
                "name": "imperium_probe",
                "description": "Return a probe value",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                },
            },
        }],
        "tool_choice": {"type": "function", "function": {"name": "imperium_probe"}},
    })
    json_payload = _chat_payload(model_id, 'Return only {"ok":true}.', "chat")
    json_payload["response_format"] = {"type": "json_object"}
    return [
        ("instruction", "chat", "/v1/chat/completions", _chat_payload(model_id, "Reply with exactly IMPERIUM_OK.", "chat"), lambda data: _content(data) == "IMPERIUM_OK"),
        ("json", "chat", "/v1/chat/completions", json_payload, _valid_json),
        ("tool", "utility", "/v1/chat/completions", tool_payload, _valid_tool),
        ("coding", "utility", "/v1/chat/completions", _chat_payload(model_id, "Write only a Python function add(a, b) that returns their sum.", "utility"), _valid_code),
        ("scribe", "scribe", "/v1/chat/completions", _chat_payload(model_id, "In at most 30 words preserve these facts: project Orion, 17 tasks, color blue.", "scribe"), _valid_scribe),
    ]


def _fingerprint(model: dict[str, Any], local: dict[str, Any], status: dict[str, Any]) -> str:
    path = Path(str(local.get("path") or ""))
    file_state: dict[str, Any] = {"path": str(path)}
    try:
        stat = path.stat()
        file_state.update({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    except OSError:
        file_state.update({"size": None, "mtime_ns": None})
    setup = status.get("setup") if isinstance(status.get("setup"), dict) else {}
    discovery = setup.get("discovery") if isinstance(setup.get("discovery"), dict) else {}
    hardware = status.get("hardware") if isinstance(status.get("hardware"), dict) else {}
    material = {
        "model_id": model.get("model_id"),
        "slot_id": model.get("slot_id"),
        "file": file_state,
        "runtime": discovery.get("managed_runtime") or discovery.get("path_runtime"),
        "hardware": {
            key: hardware.get(key)
            for key in ("gpu", "vram_mb", "ram_mb", "backend")
        },
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _resource_cost(model_id: str, local: dict[str, Any], cookbook: dict[str, Any]) -> float:
    path = str(local.get("path") or "")
    filename = Path(path).name
    report = next(
        (
            row for row in cookbook.get("models", [])
            if isinstance(row, dict)
            and (row.get("path") == path or row.get("file") == filename or row.get("file") == model_id)
        ),
        {},
    )
    return {
        "full_gpu": 0.35,
        "partial_offload": 0.65,
        "too_big": 1.0,
    }.get(str(report.get("fit") or ""), 0.5)


def _role_metrics(results: list[dict[str, Any]], resource_cost: float) -> dict[str, dict[str, Any]]:
    metrics: dict[str, dict[str, Any]] = {}
    for role in sorted({result["role"] for result in results}):
        rows = [result for result in results if result["role"] == role]
        durations = [row["latency_ms"] for row in rows if row["request_ok"]]
        rates = [row["tokens_per_second"] for row in rows if row.get("tokens_per_second")]
        metrics[role] = {
            "pass_rate": round(sum(row["passed"] for row in rows) / len(rows), 4),
            "reliability": round(sum(row["request_ok"] for row in rows) / len(rows), 4),
            "median_latency_ms": round(statistics.median(durations), 2) if durations else None,
            "tokens_per_second": round(statistics.median(rates), 2) if rates else None,
            "resource_cost_hint": resource_cost,
            "cases": len(rows),
        }
    return metrics


def evaluate_models(request: RequestFn, store: FleetStore, *, force: bool = False) -> dict[str, Any]:
    catalog = request("GET", "/routing/models", None)
    if not isinstance(catalog, dict):
        raise RuntimeError("GET /routing/models did not return an object")
    try:
        slot_rows = request("GET", "/health/slots", None)
    except RuntimeError:
        slot_rows = []
    healthy_slots = {
        str(row.get("id"))
        for row in slot_rows if isinstance(slot_rows, list) and isinstance(row, dict)
        and str(row.get("health") or "").lower() in {"healthy", "ok", "ready", "running"}
    }
    try:
        status = request("GET", "/ui/status", None)
        if not isinstance(status, dict):
            status = {}
    except RuntimeError:
        status = {}
    try:
        cookbook = request("GET", "/cookbook", None)
        if not isinstance(cookbook, dict):
            cookbook = {}
    except RuntimeError:
        cookbook = {}

    candidates: dict[str, dict[str, Any]] = {}
    for row in catalog.get("models", []):
        if isinstance(row, dict) and row.get("source") == "local_fleet" and row.get("model_id"):
            candidates.setdefault(str(row["model_id"]), row)
    setup = status.get("setup") if isinstance(status.get("setup"), dict) else {}
    discovery = setup.get("discovery") if isinstance(setup.get("discovery"), dict) else {}
    local_by_id = {
        str(row.get("id")): row
        for row in discovery.get("local_models", []) or []
        if isinstance(row, dict) and row.get("id")
    }
    previous = store.latest_model_snapshot("model_evaluation") or {}
    previous_models = {
        str(row.get("model_id")): row
        for row in previous.get("payload", {}).get("models", [])
        if isinstance(row, dict) and row.get("model_id")
    }
    evaluated: list[dict[str, Any]] = []
    for model_id, candidate in candidates.items():
        local = local_by_id.get(model_id, {})
        fingerprint = _fingerprint(candidate, local, status)
        old = previous_models.get(model_id)
        if str(candidate.get("slot_id") or "") not in healthy_slots:
            evaluated.append({
                "model_id": model_id,
                "slot_id": candidate.get("slot_id"),
                "fingerprint": fingerprint,
                "roles": {},
                "skipped_reason": "unreachable",
            })
            continue
        if not force and old and old.get("fingerprint") == fingerprint and old.get("roles"):
            evaluated.append({**old, "skipped_reason": "unchanged"})
            continue
        results = []
        embedding_only = candidate.get("role") in {"embed", "embedding"}
        for case_id, role, path, payload, validator in _cases(model_id, embedding_only):
            started = time.monotonic()
            try:
                response = request("POST", path, payload)
                if not isinstance(response, dict):
                    raise RuntimeError(f"POST {path} did not return an object")
                latency_ms = (time.monotonic() - started) * 1000
                usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
                completion_tokens = usage.get("completion_tokens")
                results.append({
                    "case": case_id,
                    "role": role,
                    "request_ok": True,
                    "passed": bool(validator(response)),
                    "latency_ms": latency_ms,
                    "tokens_per_second": (
                        float(completion_tokens) / (latency_ms / 1000)
                        if isinstance(completion_tokens, (int, float)) and latency_ms > 0
                        else None
                    ),
                })
            except RuntimeError:
                results.append({
                    "case": case_id,
                    "role": role,
                    "request_ok": False,
                    "passed": False,
                    "latency_ms": None,
                    "tokens_per_second": None,
                })
        reachable = any(result["request_ok"] for result in results)
        evaluated.append({
            "model_id": model_id,
            "slot_id": candidate.get("slot_id"),
            "fingerprint": fingerprint,
            "roles": _role_metrics(results, _resource_cost(model_id, local, cookbook)) if reachable else {},
            "skipped_reason": None if reachable else "unreachable",
        })

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "models": evaluated,
    }
    store.record_model_snapshot("model_evaluation", payload)
    return payload

"""Shared runtime context, HTTP helpers, and middleware for Imperium."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import urlparse

import aiohttp
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from local_model_router import __version__ as _VERSION
from local_model_router import structured_output
from local_model_router.harnesses import HarnessConfigError, HarnessProfiles
from local_model_router.helpers import budget_engine, usage_ledger
from local_model_router.helpers.compute_monitor import scan_hardware
from local_model_router.routing.aliases import resolve_alias
from local_model_router.routing.catalog import (
    build_slot_candidates,
    required_capabilities_from_chat_body,
    role_from_chat_body,
)
from local_model_router.setup import SetupError
from local_model_router.upstreams.registry import UpstreamConfig, match_upstream_model
from .fleet_manager import AgentIdentity, context_windows_from_slots, vram_unknown_summary
from .http_client import SharedHttpClient
from .models_listing import FetchFn, _default_fetch as _models_default_fetch
from .routing_intent import RoutingIntentRequest
from .swarm_gate import CapacitySnapshot

LEGACY_ORCHESTRATOR_ENV = "IMPERIUM_ENABLE_LEGACY_ORCHESTRATOR"
LEGACY_ORCHESTRATOR_LEGACY_ENV = "A0_LMM_ROUTER_ENABLE_LEGACY_ORCHESTRATOR"
LEGACY_ORCHESTRATOR_SUNSET = "Wed, 31 Dec 2026 23:59:59 GMT"


def legacy_orchestrator_enabled() -> bool:
    """Return True unless the legacy ``/orchestrator/*`` API is explicitly disabled."""
    raw = os.environ.get(LEGACY_ORCHESTRATOR_ENV)
    if raw is None:
        raw = os.environ.get(LEGACY_ORCHESTRATOR_LEGACY_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def resolve_app_hook(name: str, default: Any) -> Any:
    """Resolve ``local_model_router.service.app.<name>`` for test monkeypatches."""
    app_module = sys.modules.get("local_model_router.service.app")
    if app_module is not None and hasattr(app_module, name):
        return getattr(app_module, name)
    return default


SERVICE_LOGGER = logging.getLogger("local_model_router.service.app")
logger = SERVICE_LOGGER

_SERVICE_NAME = "lmm-router-observer"
_COMPUTE_CACHE_TTL_SECONDS = 5.0
_FORWARD_TIMEOUT_SECONDS = 120
# A stream is bounded by silence between reads, not total length: a long
# local generation must not be cut at the forward timeout.
_STREAM_TIMEOUT = aiohttp.ClientTimeout(
    total=None,
    sock_connect=_FORWARD_TIMEOUT_SECONDS,
    sock_read=_FORWARD_TIMEOUT_SECONDS,
)
_API_KEY_ENV = "A0_LMM_ROUTER_API_KEY"


class _ManagedStreamingResponse(StreamingResponse):
    """Guarantee upstream and request cleanup even when ASGI send disconnects."""

    def __init__(self, *args: Any, on_close: Callable[[], Awaitable[None]], **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._on_close = on_close

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            close_iterator = getattr(self.body_iterator, "aclose", None)
            try:
                if close_iterator is not None:
                    await close_iterator()
            finally:
                await self._on_close()

_ROUTING_ONLY_KEYS = {
    "routing",
    "preferred_slot",
    "role",
    "agent_id",
    "agent_type",
    "app_id",
    "task_type",
    "privacy_mode",
    "local_only",
    "cloud_allowed",
    "requires_long_context",
    "requires_tools",
    "requires_vision",
    "requires_json_mode",
    "requires_code_execution",
    "routing_strategy",
    "latency_preference",
    "quality_preference",
    "cost_preference",
    "estimated_tokens",
    "input_classification",
}


def _openai_error(
    message: str,
    code: str,
    status_code: int,
    *,
    error_type: str = "invalid_request_error",
    param: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> JSONResponse:
    body: Dict[str, Any] = {
        "error": {
            "message": message,
            "type": error_type,
            "param": param,
            "code": code,
        }
    }
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status_code, headers=headers)


def _upstream_error_text(body: Any) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or "")[:500]
        if isinstance(err, str):
            return err[:500]
        return str(body.get("message") or "")[:500]
    return str(body or "")[:500]


def _response_was_truncated(body: Any) -> bool:
    return bool(
        isinstance(body, dict)
        and any(
            isinstance(choice, dict) and choice.get("finish_reason") == "length"
            for choice in body.get("choices", [])
        )
    )


def _classify_upstream_failure(
    status: int,
    body: Any,
    *,
    pinned_harness: bool,
) -> tuple[str, int, str]:
    """Map upstream HTTP failures to stable router codes.

    Connection/timeout paths stay outside this helper. Capability gaps
    (missing mmproj / image input) must not look like an unloaded model.
    """
    del pinned_harness
    message = _upstream_error_text(body) or "upstream request failed"
    lowered = message.lower()
    if any(
        token in lowered
        for token in ("mmproj", "image input is not supported", "vision projector")
    ):
        http_status = status if 400 <= status < 500 else 400
        return "upstream_capability_missing", http_status, message
    return "upstream_error", status if status >= 400 else 502, message


def _capabilities_for_pinned_model(
    model: str,
    slots: list,
    upstream_list: list,
) -> Dict[str, bool]:
    matched = match_upstream_model(model, upstream_list)
    if matched is not None:
        upstream, bare = matched
        caps = set(upstream.effective_capabilities(bare))
        return {
            "tools": "tools" in caps,
            "vision": "vision" in caps,
            "json_mode": "json_mode" in caps or upstream.serves_inference,
        }
    target = str(model or "").strip()
    for candidate in build_slot_candidates(slots):
        if target in {candidate.slot_id, candidate.model_id, candidate.id}:
            public = candidate.public_dict().get("capabilities") or {}
            return {
                "tools": bool(public.get("tools")),
                "vision": bool(public.get("vision")),
                "json_mode": bool(public.get("json_mode", True)),
            }
    return {"tools": False, "vision": False, "json_mode": True}


def _context_for_pinned_model(model: str, slots: list[Dict[str, Any]]) -> Optional[int]:
    target = str(model or "").strip()
    for slot in slots:
        if target not in {str(slot.get("id") or ""), str(slot.get("model_id") or "")}:
            continue
        try:
            value = int(slot.get("context_size") or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _pin_model_allowed(model: str, slots: list, upstream_list: list, extra_ids: Optional[set[str]] = None) -> bool:
    """Accept configured slot models, serving upstream targets, or live aliases."""
    target = str(model or "").strip()
    if not target:
        return False
    if extra_ids and target in extra_ids:
        return True
    if match_upstream_model(target, upstream_list) is not None:
        return True
    candidates = build_slot_candidates(slots)
    if any(
        target in {candidate.slot_id, candidate.model_id, candidate.id}
        for candidate in candidates
    ):
        return True
    alias = resolve_alias(target)
    return bool(
        alias.recognized
        and alias.role
        and any(candidate.role == alias.role for candidate in candidates)
    )


_ALLOWED_LOCAL_ORIGINS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})
_CSRF_BLOCKED_MEDIA_TYPES = frozenset(
    {
        "text/plain",
        "application/x-www-form-urlencoded",
        "multipart/form-data",
    }
)
_UI_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def _origin_check_error(request: Request, api_key: str) -> Optional[JSONResponse]:
    if api_key or request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    origin = request.headers.get("origin", "").strip()
    if not origin:
        return None
    if origin.lower() == "null":
        return JSONResponse({"error": "cross_origin_forbidden"}, status_code=403)
    hostname = (urlparse(origin).hostname or "").strip().lower()
    if hostname not in _ALLOWED_LOCAL_ORIGINS:
        return JSONResponse({"error": "cross_origin_forbidden"}, status_code=403)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type in _CSRF_BLOCKED_MEDIA_TYPES:
        return JSONResponse({"error": "unsupported_media_type"}, status_code=415)
    return None


def _configured_api_key() -> str:
    return os.environ.get(_API_KEY_ENV, "").strip()


def _bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return ""
    return token.strip()


def _authorized(request: Request, api_key: str) -> bool:
    if not api_key:
        return True
    return hmac.compare_digest(_bearer_token(request), api_key)


def _unauthorized_response(request: Request) -> JSONResponse:
    path = request.url.path
    if path.startswith("/v1/") or "/v1/" in path:
        return _openai_error(
            "missing or invalid bearer token",
            "unauthorized",
            401,
            error_type="authentication_error",
        )
    return JSONResponse(
        {"error": "unauthorized", "detail": "missing or invalid bearer token"},
        status_code=401,
    )


def _dict_or_empty(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _record_upstream_usage(upstream_name: Optional[str], usage: Optional[Dict[str, Any]], model: Optional[str]) -> None:
    """Best-effort: record upstream token usage to the budget ledger. Never raises.

    Called from forward_to_upstream (the single choke point for every declared-
    upstream forward, both explicit-model-match and auto-fallback routing) right
    where the real upstream name and the real response or streamed SSE usage
    are both known.
    """
    try:
        if not upstream_name:
            return
        usage = usage or {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        if not pt and not ct:
            return
        usage_ledger.record_usage(upstream_name, tokens_in=pt, tokens_out=ct, model=model or "", source="proxy")
    except Exception:
        return


DISCLOSURE_ENFORCE_ENV = "A0_LMM_ROUTER_DISCLOSURE_ENFORCE"


def _structured_output_enforced() -> bool:
    """Whether a reply that is not valid JSON is repaired and then refused.

    Default is observe: the verdict rides in a header and telemetry, and the
    body is returned exactly as the model sent it. Enforcement changes a
    client-visible contract -- a 422 where a harness expects 200 -- so it is
    an explicit, reversible opt-in, read per request like the disclosure gate.
    """
    return os.environ.get(structured_output.ENFORCE_ENV, "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _disclosure_enforced() -> bool:
    """Whether a disclosure denial blocks the forward or only annotates it.

    Read per request so the posture can be flipped without a restart. Default
    is observe-only: the router reports what the policy would say and forwards
    unchanged, so enabling enforcement is an explicit, reversible decision.
    """
    return os.environ.get(DISCLOSURE_ENFORCE_ENV, "0").strip().lower() in {"1", "true", "yes", "on"}


def _disclosure_text(body: Dict[str, Any]) -> str:
    """Flatten a chat body's message text for classification and scanning.

    The result is used in-memory only: it is classified, scanned, and dropped.
    Nothing derived from it beyond pattern ids and class names is ever logged,
    returned, or persisted.
    """
    parts: list[str] = []
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for chunk in content:
                    if isinstance(chunk, dict) and isinstance(chunk.get("text"), str):
                        parts.append(chunk["text"])
    return "\n".join(parts)


def _pick_routing_value(
    body: Dict[str, Any],
    routing: Dict[str, Any],
    metadata: Dict[str, Any],
    key: str,
    default: Any = None,
) -> Any:
    if key in routing:
        return routing[key]
    if key in body:
        return body[key]
    if key in metadata:
        return metadata[key]
    return default


def _intent_from_chat_body(
    body: Dict[str, Any],
    agent: Optional[AgentIdentity] = None,
    *,
    app_id: Optional[str] = None,
    requested_model: Optional[str] = None,
) -> RoutingIntentRequest:
    routing = _dict_or_empty(body.get("routing"))
    metadata = _dict_or_empty(body.get("metadata"))
    role = role_from_chat_body(body)
    needs = required_capabilities_from_chat_body(body, role=role)

    payload = {
        "agent_id": _pick_routing_value(
            body, routing, metadata, "agent_id", body.get("user") or "openai_compatible_client"
        ),
        "agent_type": _pick_routing_value(body, routing, metadata, "agent_type", "custom"),
        "role": role,
        "task_type": _pick_routing_value(body, routing, metadata, "task_type", "chat"),
        "privacy_mode": _pick_routing_value(body, routing, metadata, "privacy_mode", "unknown"),
        "local_only": _pick_routing_value(body, routing, metadata, "local_only", False),
        "requires_long_context": _pick_routing_value(
            body, routing, metadata, "requires_long_context", needs.requires_long_context
        ),
        "requires_tools": _pick_routing_value(body, routing, metadata, "requires_tools", needs.requires_tools),
        "requires_vision": _pick_routing_value(body, routing, metadata, "requires_vision", needs.requires_vision),
        "requires_json_mode": _pick_routing_value(
            body, routing, metadata, "requires_json_mode", needs.requires_json_mode
        ),
        "requires_code_execution": _pick_routing_value(
            body, routing, metadata, "requires_code_execution", False
        ),
        "latency_preference": _pick_routing_value(body, routing, metadata, "latency_preference", "normal"),
        "quality_preference": _pick_routing_value(body, routing, metadata, "quality_preference", "normal"),
        "cost_preference": _pick_routing_value(body, routing, metadata, "cost_preference", "normal"),
        "routing_strategy": _pick_routing_value(body, routing, metadata, "routing_strategy", needs.strategy),
        "estimated_tokens": _pick_routing_value(body, routing, metadata, "estimated_tokens", needs.estimated_tokens),
        "preferred_slot": _pick_routing_value(body, routing, metadata, "preferred_slot", needs.preferred_slot),
        "requested_model": requested_model or body.get("model"),
        "app_id": app_id,
        "input_classification": _pick_routing_value(body, routing, metadata, "input_classification"),
        "metadata": metadata,
    }
    if agent is not None:
        payload["agent_id"] = agent.agent_id
        payload["agent_type"] = agent.agent_type
    return RoutingIntentRequest.model_validate(payload)


def _forward_payload(
    body: Dict[str, Any],
    selected_model: Optional[str],
    *,
    stream: bool,
    default_max_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    payload = {k: v for k, v in body.items() if k not in _ROUTING_ONLY_KEYS}
    payload["stream"] = stream
    payload["model"] = selected_model or payload.get("model") or "local"
    if (
        default_max_tokens is not None
        and payload.get("max_tokens") is None
        and payload.get("max_completion_tokens") is None
    ):
        payload.pop("max_tokens", None)
        payload.pop("max_completion_tokens", None)
        payload["max_tokens"] = int(default_max_tokens)
    return payload


def _slot_id_for_model(slots: list[Dict[str, Any]], model: str) -> Optional[str]:
    for slot in slots:
        if model in {
            str(slot.get("id") or ""),
            str(slot.get("model_id") or ""),
            str(slot.get("router_default_model") or ""),
        }:
            return str(slot.get("id") or "") or None
    return None


def _app_id_from(headers: Any, body: Dict[str, Any], agent: Optional[AgentIdentity]) -> str:
    """Client app id: X-App-Id header > body app_id > agent type."""
    header = str(headers.get("x-app-id", "") or "").strip()
    if header:
        return header
    body_app = body.get("app_id")
    if isinstance(body_app, str) and body_app.strip():
        return body_app.strip()
    return getattr(agent, "agent_type", "") or ""


def _forward_model_for(body: Dict[str, Any], task_type: str, decision_model: Optional[str]) -> Optional[str]:
    """Model id to send upstream.

    Recognized aliases (``auto``, ``fast``, ``coder``, …) forward the routing
    decision's model. Unrecognized names are explicit model requests — they
    pass through untouched so a Router Mode fleet (or upstream backend) can
    serve the exact id the client asked for.
    """
    resolution = resolve_alias(body.get("model"), task_type=task_type or "chat")
    if resolution.recognized:
        return decision_model
    return resolution.requested or decision_model


def _estimate_prompt_tokens(body: Dict[str, Any]) -> int:
    """Small mixed-language token estimate for OpenAI-compatible payloads."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return 0
    chars = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        chars += len(text)
    return max(0, chars // 3)


def _context_for_slot(
    slots: list[Dict[str, Any]],
    slot_id: Optional[str],
    selected_model: Optional[str] = None,
) -> Dict[str, Any]:
    windows = context_windows_from_slots(slots)
    for item in windows:
        if item.get("slot_id") == slot_id and selected_model and item.get("alias") == selected_model:
            return item
    for item in windows:
        if item.get("slot_id") == slot_id:
            return item
    return {}


def _context_headers(context: Dict[str, Any], prompt_tokens: int, selected_model: Optional[str]) -> Dict[str, str]:
    hard_ctx = context.get("hard_ctx")
    effective_ctx = context.get("effective_ctx")
    try:
        occupancy = float(prompt_tokens) / float(hard_ctx) if hard_ctx else None
    except (TypeError, ValueError, ZeroDivisionError):
        occupancy = None
    return {
        "x-selected-model": selected_model or "",
        "x-hard-ctx": str(hard_ctx) if hard_ctx else "unknown",
        "x-effective-ctx": str(effective_ctx) if effective_ctx else "unknown",
        "x-context-occupancy": f"{occupancy:.4f}" if occupancy is not None else "unknown",
        "x-a0-router-hard-ctx": str(hard_ctx) if hard_ctx else "unknown",
        "x-a0-router-effective-ctx": str(effective_ctx) if effective_ctx else "unknown",
        "x-a0-router-context-occupancy": f"{occupancy:.4f}" if occupancy is not None else "unknown",
    }


async def _stream_upstream_response(
    resp: aiohttp.ClientResponse,
    session: Optional[aiohttp.ClientSession],
    on_error: Optional[Callable[[str], Awaitable[None]]] = None,
    *,
    close_session: bool = True,
):
    saw_done = False
    tail = b""
    try:
        async for chunk in resp.content.iter_chunked(8192):
            if chunk:
                window = tail + chunk
                if b"data: [DONE]" in window:
                    saw_done = True
                else:
                    compact = window.replace(b" ", b"")
                    idx = 0
                    while True:
                        pos = compact.find(b'"finish_reason":', idx)
                        if pos == -1:
                            break
                        after = compact[pos + len(b'"finish_reason":'):]
                        if after and not after.startswith(b"null") and (
                            len(after) >= 4 or not b"null".startswith(after)
                        ):
                            # Non-null finish_reason is a usable completion signal when
                            # the upstream omits the terminal [DONE] marker.
                            saw_done = True
                            break
                        idx = pos + 1
                tail = window[-256:]
                yield chunk
        if not saw_done and on_error is not None:
            await on_error("upstream_stream_incomplete")
    except aiohttp.ClientError as exc:
        if on_error is not None:
            await on_error("upstream_stream_error")
        import json as _json

        payload = {
            "error": {
                "message": f"upstream llama.cpp stream failed: {exc}",
                "type": "server_error",
                "param": None,
                "code": "upstream_stream_error",
            }
        }
        yield f"data: {_json.dumps(payload, separators=(',', ':'))}\n\n".encode("utf-8")
    finally:
        resp.release()
        if close_session and session is not None:
            await session.close()


@dataclass
class RouterContext:
    """Shared dependencies, mutable runtime state, and wrappers for route builders."""

    observer: Any
    api_key: str
    config_writes_requested: bool
    config_writes_enabled: bool
    store: Any
    queue: Any
    prompt_cache: Any
    compute_lock: asyncio.Lock
    setup_engine: Any
    setup_token: str
    setup_api_active: Dict[str, bool]
    conf_dir: Path
    gpu_cfg: Any
    gpu_pin_ids: set[str]
    control_enabled: bool
    fleet_backend: str
    supports_start_stop: bool
    shared_manager: Any
    fleet_control: Any
    upstreams: list[Any]
    disclosure_policy: Any
    admission_queues: Dict[str, Any]
    intent_handler: Any
    app_profiles: Any
    agent_catalog: Any
    harness_profiles: HarnessProfiles
    hermes_watcher: Any
    watcher_task_ref: Dict[str, Optional[asyncio.Task[Any]]]
    harness_activity: Dict[str, Dict[str, Any]]
    agent_orchestrator: Any
    swarm_policy: Any
    swarm_registry: Any
    execution_catalog: Any
    http_client: SharedHttpClient
    models_fetch: Optional[FetchFn] = None
    compute_cache: Optional[tuple[float, dict[str, Any], dict[str, Any]]] = None
    last_observer_snapshot: Dict[str, Optional[str]] = field(default_factory=lambda: {"json": None})
    cookbook_cache: Dict[str, Any] = field(default_factory=lambda: {"key": None, "at": 0.0, "report": None})
    status_seq: Dict[str, Any] = field(default_factory=lambda: {"generation": 0, "last_ready_at": None})

    def bind(
        self,
        handler: Callable[[RouterContext, Request], Awaitable[Response]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def _bound(request: Request) -> Response:
            return await handler(self, request)

        return _bound

    def upstream_budget_status(self) -> Dict[str, str]:
        try:
            return {
                u.name: budget_engine.provider_budget(u).get("status", "unknown")
                for u in self.upstreams
                if (u.enabled or u.has_declared_limits)
            }
        except Exception:
            return {}

    def pin_hermes_gpu(self, model: str) -> None:
        try:
            self.harness_profiles = self.harness_profiles.with_connection_model("hermes", "default", model)
        except HarnessConfigError as exc:
            SERVICE_LOGGER.warning("Hermes GPU pin failed (%s)", type(exc).__name__)

    def publish_hermes_gpu(self, profile: str, model: str, effective_n_ctx: Optional[int]) -> None:
        profile_cfg = (self.gpu_cfg.profiles.get(profile) or {}) if self.gpu_cfg is not None else {}
        self.observer.set_runtime_slot(
            self.gpu_cfg.slot_id,
            model_id=model,
            effective_n_ctx=effective_n_ctx,
            configured_n_ctx=profile_cfg.get("context_size"),
            context_source=self.hermes_watcher.context_source if self.hermes_watcher is not None else None,
            supports_tools=profile_cfg.get("supports_tools"),
            supports_vision=False,
            supports_json_mode=profile_cfg.get("supports_json_mode"),
            trust_tier=profile_cfg.get("trust_tier"),
        )

    def clear_hermes_gpu(self, slot_id: str) -> None:
        self.observer.clear_runtime_slot(slot_id)

    async def fetch_slot_models(self, base_url: str) -> Optional[list[Dict[str, Any]]]:
        if self.models_fetch is not None:
            return await self.models_fetch(base_url)
        return await _models_default_fetch(base_url, http_client=self.http_client)

    async def compute_status(self) -> tuple[dict[str, Any], dict[str, Any]]:
        now = time.monotonic()
        if self.compute_cache and now - self.compute_cache[0] < _COMPUTE_CACHE_TTL_SECONDS:
            return self.compute_cache[1], self.compute_cache[2]

        scan_fn = resolve_app_hook("scan_hardware", scan_hardware)
        async with self.compute_lock:
            now = time.monotonic()
            if self.compute_cache and now - self.compute_cache[0] < _COMPUTE_CACHE_TTL_SECONDS:
                return self.compute_cache[1], self.compute_cache[2]
            try:
                hardware = await asyncio.to_thread(scan_fn)
                vram = hardware.vram_summary()
                compute = hardware.to_dict()
            except Exception as exc:
                SERVICE_LOGGER.warning("Hardware monitoring failed: %s", exc)
                vram = vram_unknown_summary()
                compute = {
                    "available": False,
                    "timestamp": None,
                    "gpus": [],
                    "cpu": None,
                    "ram": None,
                }
            self.compute_cache = (time.monotonic(), vram, compute)
            return vram, compute

    async def swarm_capacity(self) -> CapacitySnapshot:
        slots = self.observer.get_slots()
        vram, _ = await self.compute_status()
        models: set[str] = set()
        for slot in slots:
            model_id = str(slot.get("model_id") or "").strip()
            if model_id:
                models.add(model_id)
        free_gb = float(vram.get("available_gb") or 0)
        total_gb = float(vram.get("total_gb") or 0)
        occupancy = (total_gb - free_gb) / total_gb if total_gb else None
        return CapacitySnapshot(
            available_models=sorted(models),
            main_vram_occupancy=occupancy,
            free_vram_gb=free_gb,
        )

    def protected(
        self,
        handler: Callable[[Request], Awaitable[Response]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def wrapper(request: Request) -> Response:
            if not _authorized(request, self.api_key):
                return _unauthorized_response(request)
            origin_error = _origin_check_error(request, self.api_key)
            if origin_error is not None:
                return origin_error
            return await handler(request)

        return wrapper

    def deprecated(
        self,
        handler: Callable[[Request], Awaitable[Response]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def wrapper(request: Request) -> Response:
            response = await handler(request)
            response.headers["Deprecation"] = "true"
            response.headers["Sunset"] = LEGACY_ORCHESTRATOR_SUNSET
            return response

        return wrapper

    def setup_protected(
        self,
        handler: Callable[[Request], Awaitable[Response]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def wrapper(request: Request) -> Response:
            client_host = request.client.host if request.client else ""
            if client_host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
                return JSONResponse({"error": "loopback_only"}, status_code=403)
            token = request.headers.get("x-setup-token", "")
            if not hmac.compare_digest(token, self.setup_token):
                return JSONResponse({"error": "invalid_setup_token"}, status_code=401)
            if not self.setup_api_active["value"]:
                return JSONResponse({"error": "setup_api_inactive"}, status_code=410)
            try:
                return await handler(request)
            except OSError as exc:
                SERVICE_LOGGER.warning("Setup storage is unavailable: %s", exc)
                return JSONResponse(
                    {
                        "error": "setup_storage_unavailable",
                        "detail": str(exc),
                        "remediation": "Choose a writable Imperium data folder and restart setup.",
                    },
                    status_code=503,
                )

        return wrapper

    @staticmethod
    async def json_body(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception as exc:
            raise SetupError("invalid_json", "The request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise SetupError("invalid_request", "The request body must be an object")
        return payload

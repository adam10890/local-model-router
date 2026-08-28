"""
Starlette app factory for the lmm-router read-only observer service.

Usage:
    from service.app import create_app
    app = create_app("/path/to/llama_cpp_servers.yaml")

Or run via python -m local_model_router.service (see __main__.py).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

import aiohttp
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from local_model_router.a2a.card import agent_card, skill_ids
from local_model_router.apps.profiles import AppProfiles
from local_model_router.diagnostics import build_diagnostics_report, collect_doctor_checks
from local_model_router.harnesses import HarnessConfigError, HarnessProfiles, setup_manifest
from local_model_router.routing.aliases import public_aliases, resolve_alias
from local_model_router.routing.catalog import (
    apply_evaluation_hints,
    build_slot_candidates,
    required_capabilities_from_chat_body,
    role_from_chat_body,
)
from local_model_router.disclosure import (
    CONFIG_FILENAME as DISCLOSURE_CONFIG_FILENAME,
    DisclosureConfigError,
    evaluate as evaluate_disclosure,
    find_upstream_executor,
    load_policy as load_disclosure_policy,
)
from local_model_router.setup import SetupEngine, SetupError
from local_model_router.upstreams.registry import (
    UpstreamConfig,
    load_upstreams,
    match_upstream_model,
)

from .fleet_control import (
    FleetControlError,
    FleetControlHandler,
    configured_backend,
    fleet_control_enabled,
)
from .agent_orchestrator import AgentOrchestrator, OrchestratorError
from .agent_library import (
    AGENT_INPUT_MAX_BYTES,
    AgentCatalog,
    AgentRunFailed,
    AgentRunTimeout,
    AgentRunnerUnavailable,
    run_agent,
)
from .fleet_manager import (
    AgentIdentity,
    FleetQueue,
    FleetStore,
    QueueFull,
    context_windows_from_slots,
    fleet_config_from_env,
    identity_from_headers,
    slots_model_snapshot,
    vram_unknown_summary,
)
from ..helpers import budget_engine, usage_ledger
from ..helpers.compute_monitor import scan_hardware
from .models_listing import FetchFn, _default_fetch as _models_default_fetch, list_models
from .observer import ObserverBackend
from .prompt_cache import (
    InMemoryPromptCache,
    cache_key as prompt_cache_key,
    is_cacheable as prompt_is_cacheable,
    prompt_cache_enabled,
)
from .routing_intent import RoutingIntentHandler, RoutingIntentRequest
from .readiness import build_ui_status

from local_model_router import __version__ as _VERSION
logger = logging.getLogger(__name__)

_SERVICE_NAME = "lmm-router-observer"
_COMPUTE_CACHE_TTL_SECONDS = 5.0
_FORWARD_TIMEOUT_SECONDS = 120
_API_KEY_ENV = "A0_LMM_ROUTER_API_KEY"

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
    message = _upstream_error_text(body) or "upstream request failed"
    lowered = message.lower()
    if any(
        token in lowered
        for token in ("mmproj", "image input is not supported", "vision projector")
    ):
        http_status = status if 400 <= status < 500 else 400
        return "upstream_capability_missing", http_status, message
    if pinned_harness:
        return "upstream_error", status if status >= 400 else 502, message
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


def _pin_model_allowed(model: str, slots: list, upstream_list: list) -> bool:
    """Accept configured slot models, serving upstream targets, or live aliases."""
    target = str(model or "").strip()
    if not target:
        return False
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
    if request.url.path.startswith("/v1/"):
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
    where the real upstream name and the real response usage are both known.
    # ponytail: streaming responses aren't parsed for a trailing usage chunk, so
    # streamed upstream requests don't hit the ledger yet — add if budget drift
    # from streaming traffic becomes noticeable.
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


def _forward_payload(body: Dict[str, Any], selected_model: Optional[str], *, stream: bool) -> Dict[str, Any]:
    payload = {k: v for k, v in body.items() if k not in _ROUTING_ONLY_KEYS}
    payload["stream"] = stream
    payload["model"] = selected_model or payload.get("model") or "local"
    # ponytail: thinking MoEs (Carnice) spend tokens on reasoning before content;
    # Hermes often sends modest max_tokens → empty content + finish=length. Raise
    # when missing/low; keep explicit large budgets. Upgrade: per-model budgets in YAML.
    model_l = str(payload.get("model") or "").lower()
    if "carnice" in model_l:
        mt = payload.get("max_tokens")
        if mt is None or (isinstance(mt, (int, float)) and int(mt) < 2048):
            payload["max_tokens"] = 4096
    return payload


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
    session: aiohttp.ClientSession,
    on_error: Optional[Callable[[str], Awaitable[None]]] = None,
):
    saw_done = False
    try:
        async for chunk in resp.content.iter_chunked(8192):
            if chunk:
                if b"data: [DONE]" in chunk:
                    saw_done = True
                elif b'"finish_reason"' in chunk and b'"finish_reason":null' not in chunk.replace(b" ", b""):
                    # Non-null finish_reason is a usable completion signal when
                    # the upstream omits the terminal [DONE] marker.
                    saw_done = True
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
        await session.close()


def create_app(
    config_path: Optional[str] = None,
    *,
    fleet_store: Optional[FleetStore] = None,
    fleet_queue: Optional[FleetQueue] = None,
    models_fetch: Optional[FetchFn] = None,
    upstreams_path: Optional[str] = None,
    apps_path: Optional[str] = None,
    harnesses_path: Optional[str] = None,
    agents_path: Optional[str] = None,
    orchestrator: Optional[AgentOrchestrator] = None,
    setup_home: Optional[str] = None,
    setup_api_enabled: Optional[bool] = None,
) -> Starlette:
    """Return a configured Starlette app.  Safe to call multiple times (no side-effects)."""
    observer = ObserverBackend(config_path)
    api_key = _configured_api_key()
    config_writes_requested = os.environ.get("A0_LMM_ROUTER_ENABLE_CONFIG_WRITES") == "1"
    config_writes_enabled = config_writes_requested and bool(api_key)
    store = fleet_store or FleetStore()
    queue = fleet_queue or FleetQueue()
    prompt_cache = InMemoryPromptCache() if prompt_cache_enabled() else None
    compute_cache: Optional[tuple[float, dict[str, Any], dict[str, Any]]] = None
    compute_lock = asyncio.Lock()
    setup_engine = SetupEngine(home=setup_home, config_path=observer.config_path)
    setup_token = secrets.token_urlsafe(24)
    setup_api_active = {
        "value": bool(setup_api_enabled)
        if setup_api_enabled is not None
        else not Path(observer.config_path).is_file()
    }

    conf_dir = Path(observer.config_path).resolve().parent
    upstreams = load_upstreams(upstreams_path or conf_dir / "upstreams.yaml")
    try:
        disclosure_policy = load_disclosure_policy(conf_dir / DISCLOSURE_CONFIG_FILENAME)
    except DisclosureConfigError as exc:
        # A broken override must not take the router down, and must not be
        # replaced by silence: fall back to the packaged rules and say so.
        logger.warning("Disclosure rules override rejected, using packaged rules: %s", exc)
        disclosure_policy = load_disclosure_policy(None)
    def _upstream_budget_status() -> Dict[str, str]:
        # ponytail: a broken provider_budget degrades to "no budget data" for
        # this request, not a routing outage — handle() also guards this call.
        try:
            return {
                u.name: budget_engine.provider_budget(u).get("status", "unknown")
                for u in upstreams
                if (u.enabled or u.has_declared_limits)
            }
        except Exception:
            return {}

    admission_queues = {"local": queue}
    admission_queues.update({
        f"upstream:{upstream.name}": FleetQueue(upstream.max_active, upstream.max_queue)
        for upstream in upstreams
        if upstream.max_active is not None and not upstream.config_error
    })
    intent_handler = RoutingIntentHandler(
        observer,
        upstream_rows_fn=lambda: [upstream.describe() for upstream in upstreams],
        budget_status_fn=_upstream_budget_status,
        evaluation_snapshot_fn=lambda: store.latest_model_snapshot("model_evaluation"),
    )
    app_profiles = AppProfiles.load(apps_path or conf_dir / "apps.yaml")
    external_agents_path = conf_dir / "agents.yaml"
    agent_catalog = (
        AgentCatalog.load(agents_path)
        if agents_path is not None
        else AgentCatalog.load(external_agents_path)
        if external_agents_path.is_file()
        else AgentCatalog.load_packaged()
    )
    harness_profiles = HarnessProfiles.load(
        harnesses_path
        or os.environ.get("A0_LMM_ROUTER_HARNESSES_CONFIG")
        or conf_dir / "harnesses.yaml",
        legacy_path=apps_path or conf_dir / "apps.yaml",
    )
    harness_activity: Dict[str, Dict[str, Any]] = {}
    agent_orchestrator = orchestrator or AgentOrchestrator(
        repo_root=str(Path(__file__).resolve().parents[2]),
        bundles_path=str(conf_dir / "agent_orchestrator.yaml"),
    )

    control_enabled = fleet_control_enabled()
    fleet_control = FleetControlHandler(observer.config_path)
    fleet_backend = configured_backend(observer.config_path)
    supports_start_stop = fleet_backend in {"auto", "docker", "subprocess"}

    async def compute_status() -> tuple[dict[str, Any], dict[str, Any]]:
        nonlocal compute_cache
        now = time.monotonic()
        if compute_cache and now - compute_cache[0] < _COMPUTE_CACHE_TTL_SECONDS:
            return compute_cache[1], compute_cache[2]

        async with compute_lock:
            now = time.monotonic()
            if compute_cache and now - compute_cache[0] < _COMPUTE_CACHE_TTL_SECONDS:
                return compute_cache[1], compute_cache[2]
            try:
                hardware = await asyncio.to_thread(scan_hardware)
                vram = hardware.vram_summary()
                compute = hardware.to_dict()
            except Exception as exc:
                logger.warning("Hardware monitoring failed: %s", exc)
                vram = vram_unknown_summary()
                compute = {
                    "available": False,
                    "timestamp": None,
                    "gpus": [],
                    "cpu": None,
                    "ram": None,
                }
            compute_cache = (time.monotonic(), vram, compute)
            return vram, compute

    def protected(handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
        async def wrapper(request: Request) -> Response:
            if not _authorized(request, api_key):
                return _unauthorized_response(request)
            return await handler(request)

        return wrapper

    def deprecated(handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
        async def wrapper(request: Request) -> Response:
            response = await handler(request)
            response.headers["Deprecation"] = "true"
            return response

        return wrapper

    def setup_protected(handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
        async def wrapper(request: Request) -> Response:
            client_host = request.client.host if request.client else ""
            if client_host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
                return JSONResponse({"error": "loopback_only"}, status_code=403)
            token = request.headers.get("x-setup-token", "")
            if not hmac.compare_digest(token, setup_token):
                return JSONResponse({"error": "invalid_setup_token"}, status_code=401)
            if not setup_api_active["value"]:
                return JSONResponse({"error": "setup_api_inactive"}, status_code=410)
            try:
                return await handler(request)
            except OSError as exc:
                logger.warning("Setup storage is unavailable: %s", exc)
                return JSONResponse(
                    {
                        "error": "setup_storage_unavailable",
                        "detail": str(exc),
                        "remediation": "Choose a writable Imperium data folder and restart setup.",
                    },
                    status_code=503,
                )

        return wrapper

    async def _json_body(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception as exc:
            raise SetupError("invalid_json", "The request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise SetupError("invalid_request", "The request body must be an object")
        return payload

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({
            "status": "ok",
            "service": _SERVICE_NAME,
            "version": _VERSION,
            "config_path": observer.config_path,
        })

    async def slots(request: Request) -> JSONResponse:
        return JSONResponse(observer.get_slots())

    async def config_preview(request: Request) -> JSONResponse:
        return JSONResponse(observer.get_config_preview())

    async def routing_preview(request: Request) -> JSONResponse:
        role = request.query_params.get("role", "chat")
        result = await observer.get_routing_preview(role)
        return JSONResponse(result)

    async def health_slots(request: Request) -> JSONResponse:
        results = await observer.get_slots_health()
        return JSONResponse(results)

    async def _collect_readiness(
        base_url: str,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any], list[str]]:
        collection_errors: list[str] = []
        setup_result, slots_result, compute_result = await asyncio.gather(
            asyncio.to_thread(setup_engine.state),
            observer.get_slots_health(),
            compute_status(),
            return_exceptions=True,
        )
        if isinstance(setup_result, BaseException):
            logger.warning("Setup state failed: %s", setup_result)
            collection_errors.append("setup_state_unavailable")
            setup_state_payload: dict[str, Any] = {
                "hardware": {},
                "discovery": {
                    "runtime_installed": False,
                    "gguf_models": [],
                    "config_exists": Path(observer.config_path).is_file(),
                    "enabled_slots": len([slot for slot in observer.get_slots() if slot.get("enabled")]),
                },
            }
        else:
            setup_state_payload = setup_result
        if isinstance(slots_result, BaseException):
            logger.warning("Slot readiness failed: %s", slots_result)
            collection_errors.append("slots_health_unavailable")
            slot_rows = [{**slot, "health": "unknown"} for slot in observer.get_slots()]
        else:
            slot_rows = slots_result
        if isinstance(compute_result, BaseException):  # compute_status is defensive; keep a safe fallback.
            logger.warning("Compute status failed: %s", compute_result)
            collection_errors.append("hardware_unavailable")
            compute = {"available": False, "gpus": [], "cpu": None, "ram": None}
        else:
            _vram, compute = compute_result
            if compute.get("available") is not True:
                collection_errors.append("hardware_unavailable")
        payload = build_ui_status(
            setup_state=setup_state_payload,
            slots_health=slot_rows,
            compute=compute,
            base_url=base_url,
        )
        return payload, setup_state_payload, slot_rows, compute, collection_errors

    async def ui_status(request: Request) -> JSONResponse:
        payload, setup_state_payload, _slot_rows, _compute, _errors = await _collect_readiness(
            str(request.base_url).rstrip("/")
        )
        payload["setup"] = setup_state_payload
        payload["setup_api_active"] = setup_api_active["value"]
        return JSONResponse(payload)

    async def diagnostics_report(request: Request) -> JSONResponse:
        doctor_result, readiness_result = await asyncio.gather(
            asyncio.to_thread(
                collect_doctor_checks,
                observer.config_path,
                include_locations=False,
            ),
            _collect_readiness(str(request.base_url).rstrip("/")),
            return_exceptions=True,
        )
        collection_errors: list[str] = []
        if isinstance(doctor_result, BaseException):
            collection_errors.append("doctor_collection_failed")
            doctor: dict[str, Any] = {
                "ok": False,
                "checks": [
                    {
                        "code": "doctor_collection_failed",
                        "status": "fail",
                        "severity": "blocking",
                        "label": "doctor collection",
                        "detail": "diagnostic checks could not be collected",
                        "remediation": "Restart Imperium and run the checks again",
                    }
                ],
            }
        else:
            doctor = doctor_result

        if isinstance(readiness_result, BaseException):
            collection_errors.append("readiness_collection_failed")
            readiness: dict[str, Any] = {
                "overall": "unknown",
                "blocking_issues": [],
                "optional_issues": [],
                "next_action": None,
            }
            slot_rows: list[dict[str, Any]] = []
            compute: dict[str, Any] = {"available": False, "gpus": [], "cpu": None, "ram": None}
        else:
            readiness, _setup, slot_rows, compute, readiness_errors = readiness_result
            collection_errors.extend(readiness_errors)

        managed_slots: dict[str, dict[str, Any]] = {}
        if control_enabled and supports_start_stop:
            try:
                managed_slots = await fleet_control.status()
            except Exception:
                collection_errors.append("runtime_status_unavailable")

        return JSONResponse(
            build_diagnostics_report(
                generated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                imperium_version=_VERSION,
                doctor=doctor,
                readiness=readiness,
                slots=slot_rows,
                hardware=compute,
                backend=fleet_backend,
                fleet_control_enabled=control_enabled,
                fleet_control_supported=supports_start_stop,
                auth_enabled=bool(api_key),
                managed_slots=managed_slots,
                collection_errors=collection_errors,
            )
        )

    async def setup_state(request: Request) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(setup_engine.state))

    async def setup_scan(request: Request) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(setup_engine.state, refresh_hardware=True))

    async def models_directory(request: Request) -> JSONResponse:
        try:
            discovery = await asyncio.to_thread(
                setup_engine.set_models_dir,
                str((await _json_body(request)).get("path") or ""),
            )
            observer.reload()
            _cookbook_cache.update({"key": None, "at": 0.0, "report": None})
            return JSONResponse({"ok": True, "discovery": discovery})
        except SetupError as exc:
            return JSONResponse(exc.payload(), status_code=400)

    async def setup_plan(request: Request) -> JSONResponse:
        try:
            return JSONResponse(await asyncio.to_thread(setup_engine.plan, await _json_body(request)))
        except SetupError as exc:
            return JSONResponse(exc.payload(), status_code=400)

    async def setup_apply(request: Request) -> JSONResponse:
        try:
            result = await asyncio.to_thread(setup_engine.apply, await _json_body(request))
            observer.reload()
            setup_api_active["value"] = False
            return JSONResponse(result)
        except SetupError as exc:
            return JSONResponse(exc.payload(), status_code=400)

    async def setup_events(request: Request) -> Response:
        try:
            after = max(0, int(request.query_params.get("after", "0")))
        except ValueError:
            after = 0
        if "text/event-stream" not in request.headers.get("accept", ""):
            return JSONResponse(setup_engine.events(after))

        async def stream_events():
            cursor = after
            while not await request.is_disconnected():
                payload = setup_engine.events(cursor)
                for event in payload["events"]:
                    cursor = max(cursor, int(event["id"]))
                    yield f"event: setup\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(stream_events(), media_type="text/event-stream")

    async def setup_cancel(request: Request) -> JSONResponse:
        await asyncio.to_thread(setup_engine.cancel)
        return JSONResponse({"ok": True})

    async def setup_smoke(request: Request) -> JSONResponse:
        result = await asyncio.to_thread(setup_engine.smoke)
        if result.get("ok"):
            setup_api_active["value"] = False
        return JSONResponse(result)

    async def fleet_status(request: Request) -> JSONResponse:
        slots = observer.get_slots()
        snapshot = slots_model_snapshot(slots)
        context_windows = context_windows_from_slots(slots)
        store.record_model_snapshot("observer_slots", snapshot)
        agents = store.list_agents()
        vram, compute = await compute_status()
        managed_slots = await fleet_control.status() if control_enabled and supports_start_stop else {}
        runtime_fields = (
            "running",
            "healthy",
            "failure_code",
            "exit_code",
            "restart_count",
            "uptime_s",
        )
        for slot in slots:
            managed = managed_slots.get(str(slot.get("id") or ""))
            if managed:
                slot["runtime"] = {field: managed.get(field) for field in runtime_fields}
        return JSONResponse(
            {
                "ok": True,
                "service": "a0-fleet-manager",
                "version": _VERSION,
                "config": fleet_config_from_env(),
                "queue": queue.snapshot(),
                "queues": {
                    "local": {"mode": "bounded", **queue.snapshot()},
                    **{
                        f"upstream:{upstream.name}": (
                            {"mode": "bounded", **admission_queues[f"upstream:{upstream.name}"].snapshot()}
                            if f"upstream:{upstream.name}" in admission_queues
                            else {
                                "mode": "delegated",
                                "active": None,
                                "queued": None,
                                "max_active": None,
                                "max_queue": None,
                            }
                        )
                        for upstream in upstreams
                    },
                },
                "agents": {
                    "count": len(agents),
                    "items": agents,
                },
                "requests": store.request_summary(),
                "vram": vram,
                "compute": compute,
                "slots": slots,
                "model_residency": snapshot,
                "context_windows": context_windows,
                "docker_socket_enabled": control_enabled and fleet_backend == "docker",
                "fleet_control": {
                    "enabled": control_enabled,
                    "backend": fleet_backend,
                    "supports_start_stop": supports_start_stop,
                },
            }
        )

    def control_gated(
        handler: Callable[[Request], Awaitable[Response]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def wrapper(request: Request) -> Response:
            if not control_enabled:
                return _openai_error(
                    "fleet control is disabled on this router; "
                    "set A0_LMM_ROUTER_ENABLE_FLEET_CONTROL=1 and restart to enable slot start/stop",
                    "fleet_control_disabled",
                    403,
                )
            if fleet_backend == "remote":
                return _openai_error(
                    "fleet backend is externally managed; start or stop its model servers outside Imperium",
                    "remote_backend_unmanaged",
                    409,
                )
            return await handler(request)

        return wrapper

    async def _run_control(request: Request, action: str) -> Response:
        slot_id = request.path_params.get("slot_id", "")
        try:
            if action == "start":
                payload = await fleet_control.start_slot(slot_id)
            elif action == "stop":
                payload = await fleet_control.stop_slot(slot_id)
            elif action == "start_all":
                payload = await fleet_control.start_all()
            else:
                payload = await fleet_control.stop_all()
        except FleetControlError as exc:
            return _openai_error(exc.message, exc.code, exc.status_code, error_type="server_error")
        except Exception as exc:  # pragma: no cover - defensive
            return _openai_error(
                f"fleet control action failed: {exc}",
                "fleet_control_failed",
                502,
                error_type="server_error",
            )
        return JSONResponse(payload)

    _cookbook_cache: Dict[str, Any] = {"key": None, "at": 0.0, "report": None}

    async def cookbook(request: Request) -> JSONResponse:
        import yaml

        from local_model_router.cookbook.engine import build_report

        try:
            with open(observer.config_path, encoding="utf-8") as fh:
                fleet_conf = yaml.safe_load(fh) or {}
        except Exception:
            fleet_conf = {}

        models_dir = (
            str(setup_engine._read_json(setup_engine.settings_path).get("models_dir") or "").strip()
            or os.environ.get("LLAMA_MODELS_DIR", "").strip()
            or str((fleet_conf.get("global") or {}).get("models_dir", "") or "").strip()
            or str(setup_engine.models_dir)
        )
        if not models_dir or not os.path.isdir(models_dir):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "models_dir_not_configured",
                    "detail": (
                        "Point the cookbook at your GGUF folder: set LLAMA_MODELS_DIR "
                        "in .env or global.models_dir in conf/llama_cpp_servers.yaml."
                    ),
                    "models_dir": models_dir or None,
                }
            )

        now = time.time()
        force = request.query_params.get("refresh") == "1"
        if (
            not force
            and _cookbook_cache["report"] is not None
            and _cookbook_cache["key"] == models_dir
            and now - _cookbook_cache["at"] < 60
        ):
            return JSONResponse(_cookbook_cache["report"])

        report = build_report(
            models_dir,
            fleet_conf.get("hardware") or {},
            fleet_conf.get("context_policy") or {},
        )
        report["ok"] = True
        _cookbook_cache.update({"key": models_dir, "at": now, "report": report})
        return JSONResponse(report)

    async def fleet_slot_start(request: Request) -> Response:
        return await _run_control(request, "start")

    async def fleet_slot_stop(request: Request) -> Response:
        return await _run_control(request, "stop")

    async def fleet_start_all(request: Request) -> Response:
        return await _run_control(request, "start_all")

    async def fleet_stop_all(request: Request) -> Response:
        return await _run_control(request, "stop_all")

    async def fleet_agents(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "agents": store.list_agents()})

    async def fleet_agents_register(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)

        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid_request_body"}, status_code=400)

        try:
            agent = identity_from_headers(
                {
                    "x-agent-id": str(body.get("agent_id") or "anonymous").strip() or "anonymous",
                    "x-agent-type": str(body.get("agent_type") or "custom").strip() or "custom",
                    "x-priority": str(body.get("priority") or "normal").strip().lower(),
                }
            )
        except ValueError as exc:
            return JSONResponse({"error": "invalid_priority", "detail": str(exc)}, status_code=422)

        registered = store.register_agent(agent, metadata=_dict_or_empty(body.get("metadata")))
        return JSONResponse({"ok": True, "agent": registered})

    async def v1_models(request: Request) -> JSONResponse:
        listing = await list_models(observer, fetch=models_fetch)
        fetch_fn = models_fetch or _models_default_fetch
        existing_ids = {row["id"] for row in listing["data"]}
        serving_upstreams = [upstream for upstream in upstreams if upstream.serves_inference]
        fetched = await asyncio.gather(*(fetch_fn(upstream.base_url) for upstream in serving_upstreams))
        for upstream, rows in zip(serving_upstreams, fetched):
            rows = rows or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                bare_id = str(row.get("id") or "").strip()
                if not bare_id:
                    continue
                namespaced = f"{upstream.name}/{bare_id}"
                if namespaced in existing_ids:
                    continue
                existing_ids.add(namespaced)
                caps = set(upstream.effective_capabilities(bare_id))
                capabilities = {
                    "tools": "tools" in caps,
                    "vision": "vision" in caps,
                    "json_mode": "json_mode" in caps or upstream.serves_inference,
                }
                listing["data"].append({
                    "id": namespaced,
                    "object": "model",
                    "created": row.get("created") or 0,
                    "owned_by": upstream.name,
                    "capabilities": capabilities,
                    "source": "upstream",
                    "meta": {
                        "kind": "upstream_model",
                        "upstream": upstream.name,
                        "capabilities": capabilities,
                    },
                })
        return JSONResponse(listing)

    async def backends(request: Request) -> JSONResponse:
        fleet_entry = {
            "name": "local_fleet",
            "type": "llama_cpp_fleet",
            "enabled": True,
            "serves_inference": True,
            "slots": observer.get_slots(),
        }
        return JSONResponse({
            "backends": [fleet_entry] + [upstream.describe() for upstream in upstreams],
        })

    async def compute_budget_endpoint(request: Request) -> JSONResponse:
        return JSONResponse(budget_engine.compute_budget(upstreams))

    async def apps_list(request: Request) -> JSONResponse:
        return JSONResponse({"apps": app_profiles.list_profiles()})

    def harness_manifest(profile: Any) -> Dict[str, Any]:
        activity = {
            connection_name: harness_activity[f"{profile.harness_id}/{connection_name}"]
            for connection_name in profile.connections
            if f"{profile.harness_id}/{connection_name}" in harness_activity
        }
        caps_by_connection = {
            connection.name: _capabilities_for_pinned_model(
                connection.model, observer.get_slots(), upstreams
            )
            for connection in profile.connections.values()
        }
        return setup_manifest(
            profile,
            auth_required=bool(api_key),
            verification_by_connection=activity,
            capabilities_by_connection=caps_by_connection,
        )

    def resolve_harness_request(request: Request) -> tuple[Any, Any] | JSONResponse:
        harness_id = request.path_params.get("harness_id", "")
        connection_name = request.path_params.get("connection")
        try:
            profile = harness_profiles.get(harness_id)
        except (HarnessConfigError, KeyError):
            return JSONResponse(
                {"error": "unknown_harness", "harness_id": harness_id},
                status_code=404,
            )
        try:
            connection = harness_profiles.resolve(harness_id, connection_name)
        except (HarnessConfigError, KeyError):
            return JSONResponse(
                {
                    "error": "unknown_harness_connection",
                    "harness_id": harness_id,
                    "connection": connection_name,
                },
                status_code=404,
            )
        return profile, connection

    def mark_harness_activity(profile: Any, connection: Any, state: str) -> None:
        harness_activity[f"{profile.harness_id}/{connection.name}"] = {
            "state": state,
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }

    async def harnesses_list(request: Request) -> JSONResponse:
        return JSONResponse({
            "harnesses": [harness_manifest(profile) for profile in harness_profiles.list_profiles()],
            "source": harness_profiles.source,
            "config_writes_enabled": config_writes_enabled,
        })

    def config_write_denial() -> Optional[JSONResponse]:
        if not config_writes_requested:
            return JSONResponse({"error": "config_writes_disabled"}, status_code=403)
        if not api_key:
            return JSONResponse({"error": "config_write_requires_api_key"}, status_code=403)
        return None

    async def harness_create(request: Request) -> JSONResponse:
        nonlocal harness_profiles
        denial = config_write_denial()
        if denial is not None:
            return denial
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "invalid_request_body"}, status_code=400)
        try:
            harness_profiles, _backup = harness_profiles.upsert(payload)
            profile = harness_profiles.get(str(payload.get("harness_id") or ""))
        except HarnessConfigError as exc:
            return JSONResponse(
                {"error": "invalid_harness_config", "detail": str(exc)},
                status_code=422,
            )
        return JSONResponse(harness_manifest(profile), status_code=201)

    async def harness_pin(request: Request) -> JSONResponse:
        nonlocal harness_profiles
        denial = config_write_denial()
        if denial is not None:
            return denial
        harness_id = request.path_params.get("harness_id", "")
        connection_name = request.path_params.get("connection", "")
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid_request_body"}, status_code=400)
        model = str(body.get("model") or "").strip()
        if not model:
            return JSONResponse({"error": "model_required"}, status_code=422)
        try:
            harness_profiles.get(harness_id)
        except (HarnessConfigError, KeyError):
            return JSONResponse(
                {"error": "unknown_harness", "harness_id": harness_id},
                status_code=404,
            )
        try:
            harness_profiles.resolve(harness_id, connection_name)
        except (HarnessConfigError, KeyError):
            return JSONResponse(
                {
                    "error": "unknown_harness_connection",
                    "harness_id": harness_id,
                    "connection": connection_name,
                },
                status_code=404,
            )
        if not _pin_model_allowed(model, observer.get_slots(), upstreams):
            return JSONResponse(
                {
                    "error": "unknown_pin_target",
                    "model": model,
                    "detail": "No matching slot, upstream, or live alias is configured.",
                },
                status_code=422,
            )
        try:
            harness_profiles, _backup = harness_profiles.set_connection_model(
                harness_id, connection_name, model
            )
            profile = harness_profiles.get(harness_id)
        except HarnessConfigError as exc:
            return JSONResponse(
                {"error": "invalid_harness_config", "detail": str(exc)},
                status_code=422,
            )
        return JSONResponse(harness_manifest(profile))

    async def harness_detail(request: Request) -> JSONResponse:
        try:
            profile = harness_profiles.get(request.path_params.get("harness_id", ""))
        except (HarnessConfigError, KeyError):
            return JSONResponse(
                {"error": "unknown_harness", "harness_id": request.path_params.get("harness_id")},
                status_code=404,
            )
        return JSONResponse(harness_manifest(profile))

    async def harness_models(request: Request) -> JSONResponse:
        resolved = resolve_harness_request(request)
        if isinstance(resolved, JSONResponse):
            return resolved
        profile, connection = resolved
        mark_harness_activity(profile, connection, "connected")
        capabilities = _capabilities_for_pinned_model(
            connection.model, observer.get_slots(), upstreams
        )
        return JSONResponse({
            "object": "list",
            "data": [{
                "id": "local",
                "object": "model",
                "created": 0,
                "owned_by": profile.harness_id,
                "capabilities": capabilities,
                "meta": {
                    "harness_id": profile.harness_id,
                    "connection": connection.name,
                    "pinned_model": connection.model,
                    "capabilities": capabilities,
                },
            }],
        })

    async def harness_chat(request: Request) -> Response:
        resolved = resolve_harness_request(request)
        if isinstance(resolved, JSONResponse):
            return resolved
        profile, connection = resolved
        request.state.harness_connection = {
            "harness_id": profile.harness_id,
            "connection": connection.name,
            "model": connection.model,
        }
        mark_harness_activity(profile, connection, "seen")
        response = await chat_completions(request)
        if getattr(response, "status_code", 500) < 400:
            mark_harness_activity(profile, connection, "verified")
        return response

    def routing_catalog_models() -> list[Dict[str, Any]]:
        slots_with_health: list[Dict[str, Any]] = []
        for slot in apply_evaluation_hints(
            observer.get_slots(), store.latest_model_snapshot("model_evaluation")
        ):
            row = dict(slot)
            # /routing/models is read-only and fast; avoid live probes here.
            row.setdefault("health", row.get("health") or "unknown")
            slots_with_health.append(row)
        models: list[Dict[str, Any]] = []
        for alias, role in sorted(public_aliases().items()):
            normalized_role = str(role or "chat")
            models.append({
                "id": f"alias:{alias}",
                "model_id": alias,
                "source": "alias",
                "role": normalized_role,
                "backend_type": "router",
                "slot_id": None,
                "upstream_name": None,
                "context_size": 0,
                "health": "available",
                "capabilities": {
                    "auto_route": alias == "auto",
                    "tools": normalized_role in {"chat", "utility", "scribe", "task-dependent"},
                    "vision": False,
                    "json_mode": normalized_role not in {"embed", "embedding"},
                },
                "hints": {"latency_ms": None, "quality": 0.0, "resource_cost": 0.0},
                "metadata": {"kind": "alias", "maps_to_role": role},
            })
        models.extend(candidate.public_dict() for candidate in build_slot_candidates(slots_with_health))
        for upstream in upstreams:
            entry = upstream.describe()
            models.append({
                "id": upstream.name,
                "model_id": upstream.name,
                "source": "upstream",
                "role": "chat",
                "backend_type": upstream.type,
                "slot_id": None,
                "upstream_name": upstream.name,
                "context_size": 0,
                "health": "unknown" if upstream.serves_inference else "disabled",
                "capabilities": {
                    "tools": "tools" in upstream.capabilities,
                    "vision": "vision" in upstream.capabilities,
                    "json_mode": "json_mode" in upstream.capabilities or upstream.serves_inference,
                },
                "hints": {"latency_ms": None, "quality": 0.5, "resource_cost": 0.0},
                "metadata": {k: v for k, v in entry.items() if k != "base_url"},
            })
        return models

    async def routing_models(request: Request) -> JSONResponse:
        return JSONResponse({"models": routing_catalog_models()})

    async def routing_evaluations(request: Request) -> JSONResponse:
        snapshot = store.latest_model_snapshot("model_evaluation")
        return JSONResponse(snapshot or {
            "source": "model_evaluation",
            "created_at": None,
            "payload": {"schema_version": 1, "models": []},
        })

    async def routing_model_card(request: Request) -> JSONResponse:
        requested = str(request.path_params.get("model_id") or "").strip()
        for model in routing_catalog_models():
            if requested in {str(model.get("id")), str(model.get("model_id"))}:
                return JSONResponse({"model": model})
        return JSONResponse({"error": "model_not_found", "model_id": requested}, status_code=404)

    async def routing_analytics(request: Request) -> JSONResponse:
        try:
            limit = max(1, min(1000, int(request.query_params.get("limit", "50"))))
        except ValueError:
            limit = 50
        return JSONResponse(store.routing_analytics(limit=limit))

    async def agents_list(request: Request) -> JSONResponse:
        return JSONResponse({"agents": agent_catalog.public_list()})

    async def agent_run(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid_agent_input"}, status_code=400)

        agent_id = str(request.path_params.get("agent_id") or "").strip()
        definition = agent_catalog.get(agent_id)
        if definition is None:
            return JSONResponse({"error": "agent_not_found", "agent_id": agent_id}, status_code=404)

        user_input = body.get("input")
        if not isinstance(user_input, str) or not user_input.strip():
            return JSONResponse({"error": "invalid_agent_input"}, status_code=400)
        try:
            input_size = len(user_input.encode("utf-8"))
        except UnicodeEncodeError:
            return JSONResponse({"error": "invalid_agent_input"}, status_code=400)
        if input_size > AGENT_INPUT_MAX_BYTES:
            return JSONResponse({"error": "input_too_large"}, status_code=413)

        try:
            output = await run_agent(definition, user_input)
        except AgentRunnerUnavailable:
            return JSONResponse({"error": "agent_runner_unavailable"}, status_code=503)
        except AgentRunTimeout:
            return JSONResponse({"error": "agent_timeout"}, status_code=504)
        except AgentRunFailed:
            return JSONResponse({"error": "agent_model_error"}, status_code=502)
        return JSONResponse({"agent_id": definition.id, "output": output})

    def _orchestrator_error(exc: OrchestratorError) -> JSONResponse:
        return JSONResponse({"error": exc.code, "detail": exc.message}, status_code=exc.status_code)

    async def orchestrator_create_plan(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)
        try:
            return JSONResponse(agent_orchestrator.create_plan(body))
        except OrchestratorError as exc:
            return _orchestrator_error(exc)

    async def orchestrator_list_plans(request: Request) -> JSONResponse:
        return JSONResponse(agent_orchestrator.list_plans())

    async def orchestrator_summary(request: Request) -> JSONResponse:
        return JSONResponse(agent_orchestrator.summary())

    async def orchestrator_list_instances(request: Request) -> JSONResponse:
        plan_id = str(request.query_params.get("plan_id") or "").strip()
        try:
            return JSONResponse(agent_orchestrator.list_instances(plan_id=plan_id))
        except OrchestratorError as exc:
            return _orchestrator_error(exc)

    async def orchestrator_instance_upsert(request: Request) -> JSONResponse:
        instance_id = str(request.path_params.get("instance_id") or "")
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)
        try:
            return JSONResponse(agent_orchestrator.upsert_instance(instance_id, body))
        except OrchestratorError as exc:
            return _orchestrator_error(exc)

    async def orchestrator_plan_detail(request: Request) -> JSONResponse:
        plan_id = str(request.path_params.get("plan_id") or "")
        try:
            return JSONResponse(agent_orchestrator.get_plan(plan_id))
        except OrchestratorError as exc:
            return _orchestrator_error(exc)

    async def orchestrator_ticket_detail(request: Request) -> JSONResponse:
        ticket_id = str(request.path_params.get("ticket_id") or "")
        try:
            return JSONResponse(agent_orchestrator.get_ticket(ticket_id))
        except OrchestratorError as exc:
            return _orchestrator_error(exc)

    async def orchestrator_ticket_claim(request: Request) -> JSONResponse:
        ticket_id = str(request.path_params.get("ticket_id") or "")
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)
        try:
            return JSONResponse(agent_orchestrator.claim_ticket(ticket_id, body))
        except OrchestratorError as exc:
            return _orchestrator_error(exc)

    async def orchestrator_ticket_log(request: Request) -> JSONResponse:
        ticket_id = str(request.path_params.get("ticket_id") or "")
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)
        try:
            return JSONResponse(agent_orchestrator.append_ticket_log(ticket_id, body))
        except OrchestratorError as exc:
            return _orchestrator_error(exc)

    async def _orchestrator_ticket_finish(request: Request, status: str) -> JSONResponse:
        ticket_id = str(request.path_params.get("ticket_id") or "")
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)
        try:
            return JSONResponse(
                agent_orchestrator.finish_claimed_ticket(ticket_id, body, status=status)
            )
        except OrchestratorError as exc:
            return _orchestrator_error(exc)

    async def orchestrator_ticket_complete(request: Request) -> JSONResponse:
        return await _orchestrator_ticket_finish(request, "completed")

    async def orchestrator_ticket_block(request: Request) -> JSONResponse:
        return await _orchestrator_ticket_finish(request, "blocked")

    async def orchestrator_ticket_submit(request: Request) -> JSONResponse:
        ticket_id = str(request.path_params.get("ticket_id") or "")
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)
        try:
            return JSONResponse(agent_orchestrator.submit_ticket(ticket_id, body))
        except OrchestratorError as exc:
            return _orchestrator_error(exc)

    def _evaluate_upstream_disclosure(upstream: UpstreamConfig, body: Dict[str, Any]):
        """Classify this forward against the disclosure policy.

        Best-effort: a policy failure must never break a forward that would
        otherwise succeed, so it degrades to "no verdict" rather than raising.
        Returns None when no verdict could be formed.
        """
        try:
            executor = find_upstream_executor(disclosure_policy, upstreams, upstream.name)
            return evaluate_disclosure(
                disclosure_policy, executor=executor, text=_disclosure_text(body)
            )
        except Exception:
            logger.debug("Disclosure evaluation skipped for upstream %s", upstream.name)
            return None

    async def forward_to_upstream(
        upstream: UpstreamConfig,
        bare_model: str,
        body: Dict[str, Any],
        *,
        pinned_harness: Optional[Dict[str, str]] = None,
        strategy_label: str = "explicit_upstream",
        response_headers: Optional[Dict[str, str]] = None,
        finalize: Optional[Callable[..., Awaitable[None]]] = None,
    ) -> Response:
        wants_stream = body.get("stream") is True
        payload = _forward_payload(body, bare_model, stream=wants_stream)
        url = upstream.base_url + "/chat/completions"
        out_headers = {
            **(response_headers or {}),
            "x-a0-router-upstream": upstream.name,
            "x-a0-router-model": bare_model,
            "x-a0-router-requested-model": str(body.get("model") or ""),
            "x-a0-router-resolved-model": bare_model,
            "x-a0-router-strategy": strategy_label,
            "x-a0-router-cache": "BYPASS",
        }

        # Task disclosure: this is the single choke point for every declared
        # upstream forward, so it is where content meets executor trust. The
        # verdict is always reported; it only blocks when enforcement is on,
        # so the default posture is observe-and-explain, never silent change.
        disclosure = _evaluate_upstream_disclosure(upstream, body)
        if disclosure is not None:
            out_headers["x-a0-router-trust-tier"] = disclosure.executor_tier
            out_headers["x-a0-router-disclosure"] = disclosure.outcome
            out_headers["x-a0-router-disclosure-class"] = disclosure.content_class
            if not disclosure.allowed and _disclosure_enforced():
                if finalize is not None:
                    await finalize("failed", error_code="disclosure_policy_violation")
                return _openai_error(
                    (
                        f"task disclosure policy denies sending "
                        f"{disclosure.content_class} content to upstream "
                        f"'{upstream.name}' at trust tier {disclosure.executor_tier}"
                    ),
                    "disclosure_policy_violation",
                    403,
                    extra={"disclosure": disclosure.describe()},
                    headers=out_headers,
                )
        try:
            if wants_stream:
                session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=_FORWARD_TIMEOUT_SECONDS)
                )
                try:
                    resp = await session.post(url, json=payload, headers=upstream.headers())
                except Exception:
                    await session.close()
                    raise
                if resp.status >= 400:
                    try:
                        upstream_json = await resp.json(content_type=None)
                    except Exception:
                        upstream_json = await resp.text()
                    resp.release()
                    await session.close()
                    code, http_status, message = _classify_upstream_failure(
                        resp.status, upstream_json, pinned_harness=bool(pinned_harness)
                    )
                    if finalize is not None:
                        await finalize("failed", error_code=code)
                    return _openai_error(
                        message if code == "upstream_capability_missing" else (
                            f"upstream {upstream.name} stream request failed"
                        ),
                        code,
                        http_status,
                        error_type="server_error",
                        extra={
                            "upstream_status": resp.status,
                            "upstream_message": _upstream_error_text(upstream_json),
                            "upstream": upstream_json,
                            **(pinned_harness or {}),
                        },
                        headers=out_headers,
                    )

                async def managed_upstream_stream():
                    stream_state = {"failed": False}

                    async def stream_failed(code: str) -> None:
                        stream_state["failed"] = True
                        if finalize is not None:
                            await finalize("failed", error_code=code)

                    try:
                        async for chunk in _stream_upstream_response(resp, session, stream_failed):
                            yield chunk
                    except asyncio.CancelledError:
                        if finalize is not None:
                            await finalize("failed", error_code="client_disconnected")
                        raise
                    except Exception:
                        if finalize is not None:
                            await finalize("failed", error_code="upstream_stream_error")
                        raise
                    else:
                        if finalize is not None and not stream_state["failed"]:
                            await finalize("completed")

                return StreamingResponse(
                    managed_upstream_stream(),
                    status_code=resp.status,
                    media_type="text/event-stream",
                    headers={**out_headers, "cache-control": "no-cache", "x-accel-buffering": "no"},
                )

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers=upstream.headers(),
                    timeout=aiohttp.ClientTimeout(total=_FORWARD_TIMEOUT_SECONDS),
                ) as resp:
                    try:
                        upstream_json = await resp.json(content_type=None)
                    except Exception:
                        upstream_text = await resp.text()
                        if finalize is not None:
                            await finalize("failed", error_code="upstream_invalid_json")
                        return _openai_error(
                            f"upstream {upstream.name} response was not valid JSON",
                            "upstream_invalid_json",
                            502,
                            error_type="server_error",
                            extra={"upstream_status": resp.status, "upstream_body": upstream_text[:1000]},
                            headers=out_headers,
                        )
                    if pinned_harness and resp.status >= 400:
                        code, http_status, message = _classify_upstream_failure(
                            resp.status, upstream_json, pinned_harness=True
                        )
                        if finalize is not None:
                            await finalize("failed", error_code=code)
                        return _openai_error(
                            message if code == "upstream_capability_missing" else (
                                f"pinned model '{pinned_harness['pinned_model']}' request failed"
                            ),
                            code,
                            http_status if code != "harness_model_unavailable" else 503,
                            error_type="server_error",
                            extra={
                                "upstream_status": resp.status,
                                "upstream_message": _upstream_error_text(upstream_json),
                                **pinned_harness,
                            },
                            headers=out_headers,
                        )
                    if finalize is not None:
                        await finalize(
                            "completed" if resp.status < 400 else "failed",
                            error_code=None if resp.status < 400 else "upstream_error",
                            usage=_dict_or_empty(upstream_json.get("usage"))
                            if isinstance(upstream_json, dict)
                            else {},
                        )
                    if resp.status < 400:
                        _record_upstream_usage(upstream.name, _dict_or_empty(upstream_json).get("usage"), bare_model)
                    return JSONResponse(upstream_json, status_code=resp.status, headers=out_headers)
        except asyncio.CancelledError:
            if finalize is not None:
                await finalize("failed", error_code="client_disconnected")
            raise
        except aiohttp.ClientError as exc:
            if finalize is not None:
                await finalize(
                    "failed",
                    error_code="harness_model_unavailable" if pinned_harness else "upstream_unreachable",
                )
            return _openai_error(
                f"could not reach upstream {upstream.name}: {exc}",
                "harness_model_unavailable" if pinned_harness else "upstream_unreachable",
                503 if pinned_harness else 502,
                error_type="server_error",
                extra=pinned_harness,
                headers=out_headers,
            )
        except TimeoutError:
            if finalize is not None:
                await finalize(
                    "failed",
                    error_code="harness_model_unavailable" if pinned_harness else "upstream_timeout",
                )
            return _openai_error(
                f"upstream {upstream.name} timed out",
                "harness_model_unavailable" if pinned_harness else "upstream_timeout",
                503 if pinned_harness else 504,
                error_type="server_error",
                extra=pinned_harness,
                headers=out_headers,
            )
        except Exception:
            if finalize is not None:
                await finalize("failed", error_code="upstream_error")
            return _openai_error(
                f"upstream {upstream.name} request failed",
                "harness_model_unavailable" if pinned_harness else "upstream_error",
                503 if pinned_harness else 502,
                error_type="server_error",
                extra=pinned_harness,
                headers=out_headers,
            )

    async def dashboard_page(request: Request) -> HTMLResponse:
        from local_model_router.dashboard import dashboard_html

        client_host = request.client.host if request.client else ""
        token = setup_token if client_host in {"127.0.0.1", "::1", "localhost", "testclient"} else ""
        return HTMLResponse(dashboard_html(setup_token=token))

    async def dashboard_icon_file(request: Request) -> Response:
        from local_model_router.dashboard import dashboard_icon

        path = dashboard_icon(str(request.path_params.get("name") or ""))
        if path is None:
            return Response(status_code=404)
        return FileResponse(path, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})

    async def well_known_agent_card(request: Request) -> JSONResponse:
        base_url = str(request.base_url).rstrip("/")
        return JSONResponse(agent_card(base_url))

    async def a2a_skills(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid_request_body"}, status_code=400)

        skill = str(body.get("skill") or "").strip()
        skill_input = _dict_or_empty(body.get("input"))
        if skill not in skill_ids():
            return JSONResponse(
                {"error": "unknown_skill", "known_skills": sorted(skill_ids())},
                status_code=404,
            )

        if skill == "route_llm_task":
            try:
                intent = RoutingIntentRequest.model_validate({
                    "agent_id": str(skill_input.get("agent_id") or "a2a_client"),
                    "agent_type": str(skill_input.get("agent_type") or "custom"),
                    "role": skill_input.get("role"),
                    "task_type": str(skill_input.get("task_type") or "chat"),
                    "privacy_mode": str(skill_input.get("privacy_mode") or "unknown"),
                    "local_only": bool(skill_input.get("local_only", False)),
                    "requires_long_context": bool(skill_input.get("requires_long_context", False)),
                    "requires_tools": bool(skill_input.get("requires_tools", False)),
                    "estimated_tokens": skill_input.get("estimated_tokens"),
                    "preferred_slot": skill_input.get("preferred_slot"),
                })
            except ValidationError as exc:
                import json as _json

                return JSONResponse(
                    {"error": "validation_error", "detail": _json.loads(exc.json())},
                    status_code=422,
                )
            decision = await intent_handler.handle(intent)
            return JSONResponse({"skill": skill, "result": decision.model_dump()})

        if skill == "check_backend_health":
            slots_health = await observer.get_slots_health()
            return JSONResponse({
                "skill": skill,
                "result": {
                    "fleet_slots": slots_health,
                    "upstreams": [upstream.describe() for upstream in upstreams],
                },
            })

        # list_models
        listing = await list_models(observer, fetch=models_fetch)
        return JSONResponse({"skill": skill, "result": listing})

    async def routing_request(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json", "detail": "request body is not valid JSON"}, status_code=400)

        try:
            intent = RoutingIntentRequest.model_validate(body)
        except ValidationError as exc:
            import json as _json
            return JSONResponse({"error": "validation_error", "detail": _json.loads(exc.json())}, status_code=422)

        result = await intent_handler.handle(intent)
        return JSONResponse(result.model_dump())

    async def embeddings(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return _openai_error("request body is not valid JSON", "invalid_json", 400)
        if not isinstance(body, dict):
            return _openai_error("request body must be a JSON object", "invalid_request_body", 400)
        if "input" not in body:
            return _openai_error("missing required field: input", "missing_input", 400, param="input")

        routing = _dict_or_empty(body.get("routing"))
        routing.update({"role": "embed", "task_type": "embedding"})
        body["routing"] = routing
        try:
            agent = identity_from_headers(request.headers, body)
            intent = _intent_from_chat_body(body, agent, requested_model=str(body.get("model") or "embedding"))
            decision = await intent_handler.handle(intent)
        except Exception as exc:
            return _openai_error(
                f"embedding routing failed: {type(exc).__name__}: {exc}",
                "routing_error",
                500,
                error_type="server_error",
            )
        if decision.no_slot_available or not decision.selected_url:
            return _openai_error(
                "no healthy local embedding slot is available",
                "no_slot_available",
                503,
                error_type="server_error",
                extra={"routing": decision.model_dump()},
            )

        payload = {key: value for key, value in body.items() if key not in _ROUTING_ONLY_KEYS}
        payload.pop("stream", None)
        payload["model"] = _forward_model_for(body, "embedding", decision.selected_model) or "local"
        url = decision.selected_url.rstrip("/") + "/embeddings"
        headers = {
            "x-a0-router-slot-id": decision.selected_slot_id or "",
            "x-a0-router-model": decision.selected_model or "",
            "x-a0-router-strategy": decision.routing_strategy,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=_FORWARD_TIMEOUT_SECONDS),
                ) as response:
                    data = await response.json(content_type=None)
                    return JSONResponse(data, status_code=response.status, headers=headers)
        except aiohttp.ClientError as exc:
            return _openai_error(
                f"could not reach embedding slot: {exc}",
                "upstream_unreachable",
                502,
                error_type="server_error",
            )

    async def chat_completions(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return _openai_error("request body is not valid JSON", "invalid_json", 400)

        if not isinstance(body, dict):
            return _openai_error("request body must be a JSON object", "invalid_request_body", 400)

        if "messages" not in body:
            return _openai_error("missing required field: messages", "missing_messages", 400, param="messages")

        dedicated = getattr(request.state, "harness_connection", None)
        pinned_slot_id: Optional[str] = None
        try:
            agent = identity_from_headers(request.headers, body)
            requested_model = str(body.get("model") or "auto").strip() or "auto"
            if dedicated:
                app_id = dedicated["harness_id"]
                effective_model = dedicated["model"]
                for slot in observer.get_slots():
                    if effective_model in {
                        str(slot.get("id") or ""),
                        str(slot.get("model_id") or ""),
                        str(slot.get("router_default_model") or ""),
                    }:
                        pinned_slot_id = str(slot.get("id") or "")
                        body["routing"] = {
                            "preferred_slot": pinned_slot_id,
                            "role": str(slot.get("role") or "chat"),
                        }
                        break
            else:
                app_id = _app_id_from(request.headers, body, agent)
                effective_model, policy_error = app_profiles.apply(app_id, body.get("model"))
                if policy_error:
                    return _openai_error(
                        f"model '{effective_model}' is not allowed for app '{app_id}'",
                        policy_error,
                        403,
                        param="model",
                        extra={"app_id": app_id},
                    )
            body["model"] = effective_model
            intent = _intent_from_chat_body(
                body,
                agent=agent,
                app_id=app_id,
                requested_model=requested_model,
            )
        except ValidationError as exc:
            import json as _json
            return _openai_error(
                "routing metadata failed validation",
                "routing_validation_error",
                422,
                extra={"detail": _json.loads(exc.json())},
            )
        except ValueError as exc:
            return _openai_error(
                "invalid request priority; use low, normal, or high",
                "invalid_priority",
                422,
                param="X-Priority",
                extra={"detail": str(exc)},
            )

        request_id = store.create_request(agent)
        started = time.monotonic()
        admission = None
        acquired_queue: Optional[FleetQueue] = None
        admission_lane = "unresolved"
        upstream_name: Optional[str] = None
        finished = False
        route_reason_codes: list[str] = []

        def fleet_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
            current = acquired_queue.snapshot() if acquired_queue is not None else {"queued": 0}
            headers = {
                "x-a0-request-id": request_id,
                "x-a0-admission-lane": admission_lane,
                "x-a0-agent-id": agent.agent_id,
                "x-a0-agent-type": agent.agent_type,
                "x-a0-fleet-request-id": request_id,
                "x-a0-fleet-queue-depth": str(current["queued"]),
                "x-a0-fleet-vram-available-gb": "unknown",
            }
            if extra:
                headers.update(extra)
            return headers

        async def finish(
            status: str,
            *,
            error_code: Optional[str] = None,
            slot_id: Optional[str] = None,
            model: Optional[str] = None,
            decision: Any = None,
            cache_status: Optional[str] = None,
            usage: Optional[Dict[str, Any]] = None,
        ) -> None:
            nonlocal finished
            if finished:
                return
            finished = True
            duration_ms = int((time.monotonic() - started) * 1000)
            usage = usage or {}
            try:
                store.update_request(
                    request_id,
                    status=status,
                    slot_id=slot_id,
                    model=model,
                    queued_ms=admission.queued_ms if admission is not None else 0,
                    duration_ms=duration_ms,
                    error_code=error_code,
                    app_id=app_id,
                    requested_model=requested_model,
                    resolved_model=model or getattr(decision, "selected_model", None),
                    routing_strategy=getattr(decision, "routing_strategy", intent.routing_strategy),
                    selected_source=getattr(decision, "selected_source", None)
                    or ("upstream" if upstream_name else "local"),
                    upstream_name=upstream_name,
                    admission_lane=admission_lane,
                    fallback_used=getattr(decision, "fallback_used", False),
                    reason_codes=getattr(decision, "reason_codes", None) or route_reason_codes,
                    cache_status=cache_status,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                )
            finally:
                if acquired_queue is not None:
                    await acquired_queue.release()

        async def admit(lane: str) -> Optional[Response]:
            nonlocal admission_lane, admission, acquired_queue
            admission_lane = lane
            target_queue = admission_queues.get(lane)
            if target_queue is None:
                store.update_request(
                    request_id,
                    status="admitted",
                    queued_ms=0,
                    app_id=app_id,
                    requested_model=requested_model,
                    routing_strategy=intent.routing_strategy,
                    upstream_name=upstream_name,
                    admission_lane=lane,
                )
                store.record_queue_event(
                    request_id=request_id,
                    agent=agent,
                    event_type="admitted",
                    queue_depth=0,
                    active_count=0,
                    admission_lane=lane,
                )
                return None
            try:
                admission = await target_queue.acquire(agent.priority)
                acquired_queue = target_queue
            except QueueFull as exc:
                await finish("rejected", error_code="queue_full", cache_status="BYPASS")
                store.record_queue_event(
                    request_id=request_id,
                    agent=agent,
                    event_type="queue_full",
                    queue_depth=exc.queue_depth,
                    active_count=target_queue.snapshot()["active"],
                    admission_lane=lane,
                )
                return _openai_error(
                    f"admission lane '{lane}' is full",
                    "queue_full",
                    429,
                    error_type="server_error",
                    extra={
                        "queue": {
                            "admission_lane": lane,
                            "queue_depth": exc.queue_depth,
                            "max_queue": exc.max_queue,
                            "max_active": target_queue.max_active,
                        }
                    },
                    headers=fleet_headers(),
                )
            store.update_request(
                request_id,
                status="admitted",
                queued_ms=admission.queued_ms,
                app_id=app_id,
                requested_model=requested_model,
                routing_strategy=intent.routing_strategy,
                upstream_name=upstream_name,
                admission_lane=lane,
            )
            store.record_queue_event(
                request_id=request_id,
                agent=agent,
                event_type="admitted",
                queue_depth=admission.queue_depth_at_admit,
                active_count=admission.active_at_admit,
                admission_lane=lane,
            )
            return None

        upstream_match = match_upstream_model(effective_model, upstreams)
        if upstream_match is not None:
            upstream_name = upstream_match[0].name
            route_reason_codes.append("pinned_harness" if dedicated else "explicit_upstream")
            pinned_harness = None
            if dedicated:
                pinned_harness = {
                    "harness_id": dedicated["harness_id"],
                    "connection": dedicated["connection"],
                    "pinned_model": effective_model,
                }
            admission_error = await admit(f"upstream:{upstream_name}")
            if admission_error is not None:
                return admission_error
            return await forward_to_upstream(
                upstream_match[0],
                upstream_match[1],
                body,
                pinned_harness=pinned_harness,
                response_headers=fleet_headers(),
                finalize=lambda status, **kwargs: finish(
                    status, model=upstream_match[1], **kwargs
                ),
            )

        if dedicated and not pinned_slot_id:
            admission_lane = "local"
            route_reason_codes.append("pinned_harness")
            await finish("failed", error_code="harness_model_unavailable", model=effective_model)
            return _openai_error(
                f"pinned model '{effective_model}' is not configured in the local fleet",
                "harness_model_unavailable",
                503,
                error_type="server_error",
                extra={
                    "harness_id": dedicated["harness_id"],
                    "connection": dedicated["connection"],
                    "pinned_model": effective_model,
                },
                headers=fleet_headers(),
            )

        try:
            decision = await intent_handler.handle(intent)
        except Exception as exc:
            await finish("failed", error_code="routing_error")
            return _openai_error(
                f"routing decision failed: {type(exc).__name__}: {exc}",
                "routing_error",
                500,
                error_type="server_error",
                headers=fleet_headers(),
            )
        decision_body = decision.model_dump()
        if decision.no_slot_available or not decision.selected_url:
            admission_lane = "local"
            await finish("failed", error_code="no_slot_available", decision=decision, cache_status="BYPASS")
            return _openai_error(
                (
                    f"pinned model '{effective_model}' is unavailable"
                    if dedicated
                    else "no healthy local llama.cpp slot is available for this request"
                ),
                "harness_model_unavailable" if dedicated else "no_slot_available",
                503,
                error_type="server_error",
                extra={
                    "routing": decision_body,
                    **(
                        {
                            "harness_id": dedicated["harness_id"],
                            "connection": dedicated["connection"],
                            "pinned_model": effective_model,
                        }
                        if dedicated
                        else {}
                    ),
                },
                headers=fleet_headers(),
            )

        if pinned_slot_id and decision.selected_slot_id != pinned_slot_id:
            admission_lane = "local"
            await finish(
                "failed",
                error_code="harness_model_unavailable",
                decision=decision,
                cache_status="BYPASS",
            )
            return _openai_error(
                f"pinned model '{effective_model}' is unavailable",
                "harness_model_unavailable",
                503,
                error_type="server_error",
                extra={
                    "harness_id": dedicated["harness_id"],
                    "connection": dedicated["connection"],
                    "pinned_model": effective_model,
                },
                headers=fleet_headers(),
            )

        if decision.selected_upstream:
            upstream_name = decision.selected_upstream
            upstream_cfg = next(
                (
                    upstream
                    for upstream in upstreams
                    if upstream.name == decision.selected_upstream and upstream.serves_inference
                ),
                None,
            )
            if upstream_cfg is None or not decision.selected_model:
                admission_lane = f"upstream:{decision.selected_upstream}"
                await finish(
                    "failed",
                    error_code="upstream_not_configured",
                    decision=decision,
                    cache_status="BYPASS",
                )
                return _openai_error(
                    f"auto-selected upstream '{decision.selected_upstream}' is not configured",
                    "upstream_not_configured",
                    503,
                    error_type="server_error",
                    extra={"routing": decision_body},
                    headers=fleet_headers(),
                )
            admission_error = await admit(f"upstream:{upstream_name}")
            if admission_error is not None:
                return admission_error
            return await forward_to_upstream(
                upstream_cfg,
                decision.selected_model,
                body,
                strategy_label="auto_upstream_fallback",
                response_headers=fleet_headers(),
                finalize=lambda status, **kwargs: finish(
                    status,
                    model=decision.selected_model,
                    decision=decision,
                    cache_status="BYPASS",
                    **kwargs,
                ),
            )

        admission_error = await admit("local")
        if admission_error is not None:
            return admission_error
        url = decision.selected_url.rstrip("/") + "/chat/completions"
        wants_stream = body.get("stream") is True
        forward_model = _forward_model_for(body, intent.task_type, decision.selected_model)
        payload = _forward_payload(body, forward_model, stream=wants_stream)
        selected_context = _context_for_slot(
            observer.get_slots(),
            decision.selected_slot_id,
            decision.selected_model,
        )
        context_extra_headers = _context_headers(
            selected_context,
            _estimate_prompt_tokens(body),
            decision.selected_model,
        )

        def router_response_headers(cache_status: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
            headers = {
                "x-a0-router-slot-id": decision.selected_slot_id or "",
                "x-a0-router-backend": decision.selected_backend_type or "",
                "x-a0-router-requested-model": requested_model,
                "x-a0-router-resolved-model": decision.selected_model or "",
                "x-a0-router-strategy": decision.routing_strategy,
                "x-a0-router-cache": cache_status,
            }
            if decision.selected_model:
                headers["x-a0-router-model"] = decision.selected_model
            headers.update(context_extra_headers)
            if extra:
                headers.update(extra)
            return fleet_headers(headers)

        cache_status = "BYPASS"
        cache_lookup_key: Optional[str] = None
        if prompt_cache is not None and not wants_stream and prompt_is_cacheable(payload):
            cache_lookup_key = prompt_cache_key(payload, resolved_model=forward_model or decision.selected_model or "")
            cached = prompt_cache.get(cache_lookup_key)
            if cached is not None:
                cache_status = "HIT"
                await finish(
                    "completed",
                    slot_id=decision.selected_slot_id,
                    model=decision.selected_model,
                    decision=decision,
                    cache_status=cache_status,
                    usage=_dict_or_empty(cached.get("usage")),
                )
                return JSONResponse(
                    cached,
                    status_code=200,
                    headers=router_response_headers(cache_status),
                )
            cache_status = "MISS"

        try:
            if wants_stream:
                session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_FORWARD_TIMEOUT_SECONDS))
                try:
                    resp = await session.post(url, json=payload, headers={"Content-Type": "application/json"})
                except Exception:
                    await session.close()
                    raise

                headers = router_response_headers(
                    "BYPASS",
                    {
                    "cache-control": "no-cache",
                    "x-accel-buffering": "no",
                    },
                )

                if resp.status >= 400:
                    try:
                        upstream_json = await resp.json(content_type=None)
                    except Exception:
                        upstream_json = await resp.text()
                    resp.release()
                    await session.close()
                    code, http_status, message = _classify_upstream_failure(
                        resp.status, upstream_json, pinned_harness=bool(dedicated)
                    )
                    await finish(
                        "failed",
                        error_code=code,
                        slot_id=decision.selected_slot_id,
                        model=decision.selected_model,
                        decision=decision,
                        cache_status="BYPASS",
                    )
                    if code != "upstream_capability_missing":
                        message = "upstream llama.cpp stream request failed"
                        if isinstance(upstream_json, dict):
                            upstream_error = upstream_json.get("error")
                            if isinstance(upstream_error, dict) and upstream_error.get("message"):
                                message = str(upstream_error["message"])
                            elif isinstance(upstream_error, str):
                                message = upstream_error
                    return _openai_error(
                        message,
                        code,
                        http_status,
                        error_type="server_error",
                        extra={
                            "upstream_status": resp.status,
                            "upstream_message": _upstream_error_text(upstream_json),
                            "upstream": upstream_json,
                            "routing": decision_body,
                        },
                        headers=fleet_headers(),
                    )

                async def managed_stream():
                    stream_state = {"failed": False}

                    async def stream_failed(code: str) -> None:
                        stream_state["failed"] = True
                        await finish(
                            "failed",
                            error_code=code,
                            slot_id=decision.selected_slot_id,
                            model=decision.selected_model,
                            decision=decision,
                            cache_status="BYPASS",
                        )

                    try:
                        async for chunk in _stream_upstream_response(resp, session, stream_failed):
                            yield chunk
                    except asyncio.CancelledError:
                        await finish(
                            "failed",
                            error_code="client_disconnected",
                            slot_id=decision.selected_slot_id,
                            model=decision.selected_model,
                            decision=decision,
                            cache_status="BYPASS",
                        )
                        raise
                    except Exception:
                        await finish(
                            "failed",
                            error_code="upstream_stream_error",
                            slot_id=decision.selected_slot_id,
                            model=decision.selected_model,
                            decision=decision,
                            cache_status="BYPASS",
                        )
                        raise
                    else:
                        if not stream_state["failed"]:
                            await finish(
                                "completed",
                                slot_id=decision.selected_slot_id,
                                model=decision.selected_model,
                                decision=decision,
                                cache_status="BYPASS",
                            )

                return StreamingResponse(
                    managed_stream(),
                    status_code=resp.status,
                    media_type="text/event-stream",
                    headers=headers,
                )

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=_FORWARD_TIMEOUT_SECONDS),
                ) as resp:
                    try:
                        upstream_json = await resp.json(content_type=None)
                    except Exception:
                        upstream_text = await resp.text()
                        await finish(
                            "failed",
                            error_code="upstream_invalid_json",
                            slot_id=decision.selected_slot_id,
                            model=decision.selected_model,
                            decision=decision,
                            cache_status=cache_status,
                        )
                        return _openai_error(
                            "upstream llama.cpp response was not valid JSON",
                            "upstream_invalid_json",
                            502,
                            error_type="server_error",
                            extra={"upstream_status": resp.status, "upstream_body": upstream_text[:1000]},
                            headers=fleet_headers(),
                        )

                    headers = router_response_headers(cache_status)

                    if resp.status >= 400:
                        code, http_status, message = _classify_upstream_failure(
                            resp.status, upstream_json, pinned_harness=bool(dedicated)
                        )
                        if code != "upstream_capability_missing":
                            message = "upstream llama.cpp request failed"
                            if isinstance(upstream_json, dict):
                                upstream_error = upstream_json.get("error")
                                if isinstance(upstream_error, dict) and upstream_error.get("message"):
                                    message = str(upstream_error["message"])
                                elif isinstance(upstream_error, str):
                                    message = upstream_error
                        await finish(
                            "failed",
                            error_code=code,
                            slot_id=decision.selected_slot_id,
                            model=decision.selected_model,
                            decision=decision,
                            cache_status=cache_status,
                            usage=_dict_or_empty(upstream_json.get("usage"))
                            if isinstance(upstream_json, dict)
                            else {},
                        )
                        return _openai_error(
                            message,
                            code,
                            http_status,
                            error_type="server_error",
                            extra={
                                "upstream_status": resp.status,
                                "upstream_message": _upstream_error_text(upstream_json),
                                "upstream": upstream_json,
                                "routing": decision_body,
                            },
                            headers=fleet_headers(),
                        )

                    await finish(
                        "completed",
                        slot_id=decision.selected_slot_id,
                        model=decision.selected_model,
                        decision=decision,
                        cache_status=cache_status,
                        usage=_dict_or_empty(upstream_json.get("usage")),
                    )
                    if cache_status == "MISS" and cache_lookup_key is not None and prompt_cache is not None:
                        prompt_cache.set(cache_lookup_key, upstream_json)
                    return JSONResponse(upstream_json, status_code=resp.status, headers=headers)
        except asyncio.CancelledError:
            await finish(
                "failed",
                error_code="client_disconnected",
                slot_id=decision.selected_slot_id,
                model=decision.selected_model,
                decision=decision,
                cache_status=cache_status,
            )
            raise
        except aiohttp.ClientError as exc:
            await finish(
                "failed",
                error_code="upstream_unreachable",
                slot_id=decision.selected_slot_id,
                model=decision.selected_model,
                decision=decision,
                cache_status=cache_status,
            )
            return _openai_error(
                f"could not reach selected llama.cpp slot: {exc}",
                "upstream_unreachable",
                502,
                error_type="server_error",
                extra={"routing": decision_body},
                headers=fleet_headers(),
            )
        except TimeoutError:
            await finish(
                "failed",
                error_code="upstream_timeout",
                slot_id=decision.selected_slot_id,
                model=decision.selected_model,
                decision=decision,
                cache_status=cache_status,
            )
            return _openai_error(
                "selected llama.cpp slot timed out",
                "upstream_timeout",
                504,
                error_type="server_error",
                extra={"routing": decision_body},
                headers=fleet_headers(),
            )
        except Exception:
            await finish(
                "failed",
                error_code="upstream_error",
                slot_id=decision.selected_slot_id,
                model=decision.selected_model,
                decision=decision,
                cache_status=cache_status,
            )
            return _openai_error(
                "selected llama.cpp slot request failed",
                "upstream_error",
                502,
                error_type="server_error",
                extra={"routing": decision_body},
                headers=fleet_headers(),
            )

    routes = [
        Route("/health", health),
        Route("/slots", protected(slots)),
        Route("/config/preview", protected(config_preview)),
        Route("/routing/preview", protected(routing_preview)),
        Route("/health/slots", protected(health_slots)),
        Route("/ui/status", protected(ui_status)),
        Route("/diagnostics/report", protected(diagnostics_report)),
        Route("/models/directory", protected(models_directory), methods=["POST"]),
        Route("/setup/state", setup_protected(setup_state)),
        Route("/setup/scan", setup_protected(setup_scan), methods=["POST"]),
        Route("/setup/plan", setup_protected(setup_plan), methods=["POST"]),
        Route("/setup/apply", setup_protected(setup_apply), methods=["POST"]),
        Route("/setup/events", setup_protected(setup_events)),
        Route("/setup/cancel", setup_protected(setup_cancel), methods=["POST"]),
        Route("/setup/smoke", setup_protected(setup_smoke), methods=["POST"]),
        Route("/fleet/status", protected(fleet_status)),
        Route("/fleet/agents", protected(fleet_agents)),
        Route("/fleet/agents/register", protected(fleet_agents_register), methods=["POST"]),
        Route("/fleet/start", protected(control_gated(fleet_start_all)), methods=["POST"]),
        Route("/fleet/stop", protected(control_gated(fleet_stop_all)), methods=["POST"]),
        Route("/fleet/slots/{slot_id}/start", protected(control_gated(fleet_slot_start)), methods=["POST"]),
        Route("/fleet/slots/{slot_id}/stop", protected(control_gated(fleet_slot_stop)), methods=["POST"]),
        Route("/routing/request", protected(routing_request), methods=["POST"]),
        Route("/routing/models", protected(routing_models)),
        Route("/routing/evaluations", protected(routing_evaluations)),
        Route("/routing/models/{model_id:path}", protected(routing_model_card)),
        Route("/routing/analytics", protected(routing_analytics)),
        Route("/orchestrator/plans", protected(deprecated(orchestrator_create_plan)), methods=["POST"]),
        Route("/orchestrator/plans", protected(deprecated(orchestrator_list_plans))),
        Route("/orchestrator/summary", protected(deprecated(orchestrator_summary))),
        Route("/orchestrator/instances", protected(deprecated(orchestrator_list_instances))),
        Route("/orchestrator/instances/{instance_id}", protected(deprecated(orchestrator_instance_upsert)), methods=["POST"]),
        Route("/orchestrator/plans/{plan_id}", protected(deprecated(orchestrator_plan_detail))),
        Route("/orchestrator/tickets/{ticket_id}/claim", protected(deprecated(orchestrator_ticket_claim)), methods=["POST"]),
        Route("/orchestrator/tickets/{ticket_id}/log", protected(deprecated(orchestrator_ticket_log)), methods=["POST"]),
        Route("/orchestrator/tickets/{ticket_id}/complete", protected(deprecated(orchestrator_ticket_complete)), methods=["POST"]),
        Route("/orchestrator/tickets/{ticket_id}/block", protected(deprecated(orchestrator_ticket_block)), methods=["POST"]),
        Route("/orchestrator/tickets/{ticket_id}/submit", protected(deprecated(orchestrator_ticket_submit)), methods=["POST"]),
        Route("/orchestrator/tickets/{ticket_id}", protected(deprecated(orchestrator_ticket_detail))),
        Route("/backends", protected(backends)),
        Route("/compute/budget", protected(compute_budget_endpoint)),
        Route("/apps", protected(apps_list)),
        Route("/agents", protected(agents_list)),
        Route("/agents/{agent_id}/runs", protected(agent_run), methods=["POST"]),
        Route("/harnesses", protected(harnesses_list)),
        Route("/harnesses", protected(harness_create), methods=["POST"]),
        Route(
            "/harnesses/{harness_id}/connections/{connection}",
            protected(harness_pin),
            methods=["PATCH"],
        ),
        Route("/harnesses/{harness_id}/v1/models", protected(harness_models)),
        Route(
            "/harnesses/{harness_id}/v1/chat/completions",
            protected(harness_chat),
            methods=["POST"],
        ),
        Route("/harnesses/{harness_id}/{connection}/v1/models", protected(harness_models)),
        Route(
            "/harnesses/{harness_id}/{connection}/v1/chat/completions",
            protected(harness_chat),
            methods=["POST"],
        ),
        Route("/harnesses/{harness_id}", protected(harness_detail)),
        Route("/cookbook", protected(cookbook)),
        Route("/.well-known/agent-card.json", well_known_agent_card),
        Route("/a2a", protected(a2a_skills), methods=["POST"]),
        Route("/ui", dashboard_page),
        Route("/ui/icons/{name}", dashboard_icon_file),
        Route("/v1/models", protected(v1_models)),
        Route("/v1/chat/completions", protected(chat_completions), methods=["POST"]),
        Route("/v1/embeddings", protected(embeddings), methods=["POST"]),
    ]

    app = Starlette(routes=routes)
    app.state.setup_token = setup_token
    app.state.setup_engine = setup_engine
    return app

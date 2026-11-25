"""Setup wizard, readiness, and sanitized diagnostics routes."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from local_model_router.diagnostics import build_diagnostics_report, collect_doctor_checks
from local_model_router.setup import SetupError
from .context import (
    SERVICE_LOGGER,
    _VERSION,
    RouterContext,
    _dict_or_empty,
    _pin_model_allowed,
    resolve_app_hook,
)
from .fleet_manager import vram_unknown_summary
from .readiness import (
    active_slot_from_health,
    build_ui_status,
    inspect_running_llama_models,
    is_generic_model_id,
)


async def _collect_readiness(
    ctx: RouterContext,
    base_url: str,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any], list[str]]:
    collection_errors: list[str] = []
    setup_result, slots_result, compute_result = await asyncio.gather(
        asyncio.to_thread(ctx.setup_engine.state),
        ctx.observer.get_slots_health(),
        ctx.compute_status(),
        return_exceptions=True,
    )
    if isinstance(setup_result, BaseException):
        SERVICE_LOGGER.warning("Setup state failed: %s", setup_result)
        collection_errors.append("setup_state_unavailable")
        setup_state_payload: dict[str, Any] = {
            "hardware": {},
            "discovery": {
                "runtime_installed": False,
                "gguf_models": [],
                "config_exists": Path(ctx.observer.config_path).is_file(),
                "enabled_slots": len([slot for slot in ctx.observer.get_slots() if slot.get("enabled")]),
            },
        }
    else:
        setup_state_payload = setup_result
    if isinstance(slots_result, BaseException):
        SERVICE_LOGGER.warning("Slot readiness failed: %s", slots_result)
        collection_errors.append("slots_health_unavailable")
        slot_rows = [{**slot, "health": "unknown"} for slot in ctx.observer.get_slots()]
    else:
        slot_rows = slots_result
    if isinstance(compute_result, BaseException):  # compute_status is defensive; keep a safe fallback.
        SERVICE_LOGGER.warning("Compute status failed: %s", compute_result)
        collection_errors.append("hardware_unavailable")
        compute = {"available": False, "gpus": [], "cpu": None, "ram": None}
    else:
        _vram, compute = compute_result
        if compute.get("available") is not True:
            collection_errors.append("hardware_unavailable")
    process_models: dict[int, str] = {}
    try:
        process_models = await asyncio.to_thread(resolve_app_hook("inspect_running_llama_models", inspect_running_llama_models))
    except Exception as exc:
        SERVICE_LOGGER.warning("llama.cpp process inspect failed (%s)", type(exc).__name__)
        collection_errors.append("process_inspect_unavailable")
    active_slot = active_slot_from_health(slot_rows)
    try:
        active_port = int(active_slot.get("port") or 0)
    except (TypeError, ValueError):
        active_port = 0
    live_ids: list[str] = []
    if (
        not process_models.get(active_port)
        and is_generic_model_id(str(active_slot.get("model_id") or ""))
        and active_slot.get("base_url")
    ):
        fetch_fn = ctx.fetch_slot_models
        try:
            rows = await fetch_fn(str(active_slot["base_url"]))
        except Exception:
            rows = None
        live_ids = [
            str(row.get("id"))
            for row in (rows or [])
            if isinstance(row, dict) and row.get("id")
        ]
    harness_pin_checks: list[dict[str, Any]] = []
    if slot_rows or any(getattr(u, "serves_inference", False) for u in ctx.upstreams):
        harness_pin_checks = [
            {
                "harness_id": profile.harness_id,
                "connections": [
                    {
                        "name": connection.name,
                        "pin_valid": _pin_model_allowed(
                            connection.model,
                            slot_rows,
                            ctx.upstreams,
                            extra_ids=ctx.gpu_pin_ids,
                        ),
                    }
                    for connection in profile.connections.values()
                ],
            }
            for profile in ctx.harness_profiles.list_profiles()
        ]
    payload = build_ui_status(
        setup_state=setup_state_payload,
        slots_health=slot_rows,
        compute=compute,
        base_url=base_url,
        process_models=process_models,
        live_ids=live_ids,
        harnesses=harness_pin_checks,
    )
    return payload, setup_state_payload, slot_rows, compute, collection_errors


async def ui_status(ctx: RouterContext, request: Request) -> JSONResponse:
    gen = int(ctx.status_seq.get("generation") or 0) + 1
    ctx.status_seq["generation"] = gen
    payload, setup_state_payload, _slot_rows, _compute, _errors = await _collect_readiness(ctx, 
        str(request.base_url).rstrip("/")
    )
    checked_at = datetime.now(timezone.utc).isoformat()
    if payload.get("overall") == "ready":
        ctx.status_seq["last_ready_at"] = checked_at
    payload["freshness"] = {
        "generation": gen,
        "checked_at": checked_at,
        "last_ready_at": ctx.status_seq.get("last_ready_at"),
        "stale_after_seconds": 30,
    }
    payload["setup"] = setup_state_payload
    payload["setup_api_active"] = ctx.setup_api_active["value"]
    if ctx.hermes_watcher is not None:
        payload["hermes_gpu"] = ctx.hermes_watcher.snapshot()
    return JSONResponse(payload)


async def diagnostics_report(ctx: RouterContext, request: Request) -> JSONResponse:
    doctor_result, readiness_result = await asyncio.gather(
        asyncio.to_thread(
            resolve_app_hook("collect_doctor_checks", collect_doctor_checks),
            ctx.observer.config_path,
            include_locations=False,
        ),
        _collect_readiness(ctx, str(request.base_url).rstrip("/")),
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
    if ctx.control_enabled and ctx.supports_start_stop:
        try:
            managed_slots = await ctx.fleet_control.status()
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
            backend=ctx.fleet_backend,
            fleet_control_enabled=ctx.control_enabled,
            fleet_control_supported=ctx.supports_start_stop,
            auth_enabled=bool(ctx.api_key),
            managed_slots=managed_slots,
            collection_errors=collection_errors,
        )
    )


async def setup_state(ctx: RouterContext, request: Request) -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(ctx.setup_engine.state))


async def setup_scan(ctx: RouterContext, request: Request) -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(ctx.setup_engine.state, refresh_hardware=True))


async def models_directory(ctx: RouterContext, request: Request) -> JSONResponse:
    try:
        discovery = await asyncio.to_thread(
            ctx.setup_engine.set_models_dir,
            str((await ctx.json_body(request)).get("path") or ""),
        )
        ctx.observer.reload()
        ctx.cookbook_cache.update({"key": None, "at": 0.0, "report": None})
        return JSONResponse({"ok": True, "discovery": discovery})
    except SetupError as exc:
        return JSONResponse(exc.payload(), status_code=400)


async def setup_plan(ctx: RouterContext, request: Request) -> JSONResponse:
    try:
        return JSONResponse(await asyncio.to_thread(ctx.setup_engine.plan, await ctx.json_body(request)))
    except SetupError as exc:
        return JSONResponse(exc.payload(), status_code=400)


async def setup_apply(ctx: RouterContext, request: Request) -> JSONResponse:
    try:
        result = await asyncio.to_thread(ctx.setup_engine.apply, await ctx.json_body(request))
        ctx.observer.reload()
        ctx.setup_api_active["value"] = False
        return JSONResponse(result)
    except SetupError as exc:
        return JSONResponse(exc.payload(), status_code=400)


async def setup_events(ctx: RouterContext, request: Request) -> Response:
    try:
        after = max(0, int(request.query_params.get("after", "0")))
    except ValueError:
        after = 0
    if "text/event-stream" not in request.headers.get("accept", ""):
        try:
            payload = ctx.setup_engine.events(after)
        except Exception:
            SERVICE_LOGGER.warning("Setup event lookup failed", exc_info=True)
            return JSONResponse(
                {
                    "error": "setup_events_unavailable",
                    "events": [],
                    "cancelled": False,
                    "history": "unavailable",
                    "resync_required": True,
                    "cursor": 0,
                    "oldest_id": None,
                    "latest_id": 0,
                },
                status_code=503,
            )
        return JSONResponse(payload)

    async def stream_events():
        cursor = after
        while not await request.is_disconnected():
            try:
                payload = ctx.setup_engine.events(cursor)
            except Exception:
                SERVICE_LOGGER.warning("Setup event stream lookup failed", exc_info=True)
                err_payload = {
                    "error": "setup_events_unavailable",
                    "history": "unavailable",
                    "resync_required": True,
                }
                yield f"event: resync\ndata: {json.dumps(err_payload, ensure_ascii=False)}\n\n"
                return
            if payload.get("resync_required"):
                resync_meta = {
                    "history": payload.get("history", "lost"),
                    "resync_required": True,
                    "cursor": int(payload.get("cursor") or 0),
                    "oldest_id": payload.get("oldest_id"),
                    "latest_id": int(payload.get("latest_id") or 0),
                    "cancelled": bool(payload.get("cancelled")),
                }
                yield f"event: resync\ndata: {json.dumps(resync_meta, ensure_ascii=False)}\n\n"
                for event in payload.get("events") or ():
                    yield f"event: setup\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                cursor = int(payload.get("cursor") or 0)
            else:
                for event in payload.get("events") or ():
                    event_id = int(event["id"])
                    if event_id <= cursor:
                        continue
                    cursor = event_id
                    yield f"event: setup\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if await request.is_disconnected():
                break
            await asyncio.sleep(0.5)
        note_lost = getattr(ctx.setup_engine, "note_transport_lost", None)
        if callable(note_lost):
            note_lost()

    return StreamingResponse(stream_events(), media_type="text/event-stream")


async def setup_cancel(ctx: RouterContext, request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    cause = str(body.get("cause") or "ui_cancel")
    actor = str(body.get("actor") or "dashboard")
    req_id = request.headers.get("x-request-id")
    stop = await asyncio.to_thread(
        ctx.setup_engine.cancel,
        cause=cause,
        actor=actor,
        request_id=req_id,
    )
    return JSONResponse({"ok": True, "stop": stop})


async def setup_smoke(ctx: RouterContext, request: Request) -> JSONResponse:
    result = await asyncio.to_thread(ctx.setup_engine.smoke)
    if result.get("ok"):
        ctx.setup_api_active["value"] = False
    return JSONResponse(result)



def build_setup_routes(ctx: RouterContext) -> list[Route]:
    return [
        Route("/ui/status", ctx.protected(ctx.bind(ui_status))),
        Route("/diagnostics/report", ctx.protected(ctx.bind(diagnostics_report))),
        Route("/models/directory", ctx.protected(ctx.bind(models_directory)), methods=["POST"]),
        Route("/setup/state", ctx.setup_protected(ctx.bind(setup_state))),
        Route("/setup/scan", ctx.setup_protected(ctx.bind(setup_scan)), methods=["POST"]),
        Route("/setup/plan", ctx.setup_protected(ctx.bind(setup_plan)), methods=["POST"]),
        Route("/setup/apply", ctx.setup_protected(ctx.bind(setup_apply)), methods=["POST"]),
        Route("/setup/events", ctx.setup_protected(ctx.bind(setup_events))),
        Route("/setup/cancel", ctx.setup_protected(ctx.bind(setup_cancel)), methods=["POST"]),
        Route("/setup/smoke", ctx.setup_protected(ctx.bind(setup_smoke)), methods=["POST"]),
    ]

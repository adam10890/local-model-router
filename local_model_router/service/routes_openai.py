"""OpenAI-compatible /v1/models, /v1/chat/completions, and /v1/embeddings routes."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Dict, Optional

import aiohttp
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from local_model_router import structured_output
from local_model_router.disclosure import evaluate as evaluate_disclosure, find_upstream_executor
from local_model_router.harnesses import HarnessConfigError
from local_model_router.routing.aliases import public_aliases, resolve_alias
from local_model_router.routing.catalog import required_capabilities_from_chat_body
from local_model_router.upstreams.registry import UpstreamConfig, match_upstream_model
from .context import (
    _FORWARD_TIMEOUT_SECONDS,
    _ROUTING_ONLY_KEYS,
    SERVICE_LOGGER,
    RouterContext,
    _ManagedStreamingResponse,
    _app_id_from,
    _classify_upstream_failure,
    _context_for_slot,
    _context_headers,
    _dict_or_empty,
    _disclosure_enforced,
    _disclosure_text,
    _estimate_prompt_tokens,
    _forward_model_for,
    _forward_payload,
    _intent_from_chat_body,
    _openai_error,
    _record_upstream_usage,
    _response_was_truncated,
    _slot_id_for_model,
    _stream_upstream_response,
    _structured_output_enforced,
    _upstream_error_text,
    resolve_app_hook,
)
from .fleet_manager import FleetQueue, QueueFull, identity_from_headers
from .hermes_model_watcher import public_hermes_model_ids
from .models_listing import list_models
from .prompt_cache import cache_key as prompt_cache_key, is_cacheable as prompt_is_cacheable


def _evaluate_upstream_disclosure(ctx: RouterContext, upstream: UpstreamConfig, body: Dict[str, Any]):
    """Classify this forward against the disclosure policy.

    Best-effort: a policy failure must never break a forward that would
    otherwise succeed, so it degrades to "no verdict" rather than raising.
    Returns None when no verdict could be formed.
    """
    try:
        executor = find_upstream_executor(ctx.disclosure_policy, ctx.upstreams, upstream.name)
        return evaluate_disclosure(
            ctx.disclosure_policy, executor=executor, text=_disclosure_text(body)
        )
    except Exception:
        SERVICE_LOGGER.debug("Disclosure evaluation skipped for upstream %s", upstream.name)
        return None


async def check_structured_output(
    forward_payload: Dict[str, Any],
    reply: Any,
    resend: Optional[Callable[[Dict[str, Any]], Awaitable[Any]]] = None,
    *,
    streamed: bool = False,
) -> tuple[Any, str]:
    """Check a reply the client asked for as JSON; repair once if enforcing.

    Returns the reply to use and a reason code (empty when JSON was never
    requested, so the common path is untouched). Never raises: if the
    repair attempt fails for any reason the original reply stands, because
    a validation feature must not turn a served answer into an outage.
    """
    if not structured_output.json_output_requested(forward_payload):
        return reply, ""
    if streamed:
        # The stream is never buffered, so there is nothing to parse.
        return reply, structured_output.UNSUPPORTED

    result = structured_output.validate_reply(reply)
    if result.ok:
        return reply, structured_output.VALID
    if resend is None or not _structured_output_enforced():
        return reply, structured_output.FAILED

    try:
        repaired = await resend(structured_output.repair_body(forward_payload, result))
    except Exception:
        SERVICE_LOGGER.debug("Structured-output repair attempt failed; keeping the original reply")
        return reply, structured_output.FAILED
    if repaired is None:
        return reply, structured_output.FAILED
    if structured_output.validate_reply(repaired).ok:
        return repaired, structured_output.REPAIRED
    return repaired, structured_output.FAILED


async def forward_to_upstream(
    ctx: RouterContext,
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
    disclosure = _evaluate_upstream_disclosure(ctx, upstream, body)
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

            stream_state = {"failed": False, "finalized": False}

            async def finalize_once(status: str, **kwargs: Any) -> None:
                if stream_state["finalized"]:
                    return
                stream_state["finalized"] = True
                if finalize is not None:
                    await finalize(status, **kwargs)

            async def managed_upstream_stream():
                async def stream_failed(code: str) -> None:
                    stream_state["failed"] = True
                    await finalize_once("failed", error_code=code)

                try:
                    async for chunk in _stream_upstream_response(resp, session, stream_failed):
                        yield chunk
                except asyncio.CancelledError:
                    await finalize_once("failed", error_code="client_disconnected")
                    raise
                except Exception:
                    await finalize_once("failed", error_code="upstream_stream_error")
                    raise
                else:
                    if not stream_state["failed"]:
                        await finalize_once("completed")
                finally:
                    if not stream_state["finalized"]:
                        await finalize_once("failed", error_code="client_disconnected")

            async def close_managed_upstream_stream() -> None:
                try:
                    resp.release()
                except Exception:
                    pass
                try:
                    await session.close()
                finally:
                    if not stream_state["finalized"]:
                        await finalize_once("failed", error_code="client_disconnected")

            return _ManagedStreamingResponse(
                managed_upstream_stream(),
                status_code=resp.status,
                media_type="text/event-stream",
                headers={**out_headers, "cache-control": "no-cache", "x-accel-buffering": "no"},
                on_close=close_managed_upstream_stream,
            )

        async with ctx.http_client.request_session() as session:
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
                usage = (
                    _dict_or_empty(upstream_json.get("usage"))
                    if isinstance(upstream_json, dict)
                    else {}
                )
                if resp.status >= 400:
                    if finalize is not None:
                        await finalize("failed", error_code="upstream_error", usage=usage)
                    return JSONResponse(upstream_json, status_code=resp.status, headers=out_headers)

                async def resend_upstream(repair_payload: Dict[str, Any]) -> Any:
                    # Rides the admission lease this request already holds:
                    # a second acquire would deadlock at max_active=1.
                    async with session.post(
                        url,
                        json=repair_payload,
                        headers=upstream.headers(),
                        timeout=aiohttp.ClientTimeout(total=_FORWARD_TIMEOUT_SECONDS),
                    ) as retry_resp:
                        if retry_resp.status >= 400:
                            return None
                        return await retry_resp.json(content_type=None)

                upstream_json, structured_code = await check_structured_output(
                    payload, upstream_json, resend_upstream
                )
                if structured_code:
                    out_headers["x-a0-router-structured-output"] = structured_code
                usage = (
                    _dict_or_empty(upstream_json.get("usage"))
                    if isinstance(upstream_json, dict)
                    else {}
                )
                if structured_code == structured_output.FAILED and _structured_output_enforced():
                    if finalize is not None:
                        await finalize(
                            "failed",
                            error_code=structured_output.FAILED,
                            usage=usage,
                        )
                    return _openai_error(
                        "the model did not return valid JSON for a response_format request",
                        structured_output.FAILED,
                        422,
                        headers=out_headers,
                    )
                if finalize is not None:
                    await finalize("completed", usage=usage)
                resolve_app_hook("_record_upstream_usage", _record_upstream_usage)(upstream.name, usage, bare_model)
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


async def v1_models(ctx: RouterContext, request: Request) -> JSONResponse:
    listing = await list_models(ctx.observer, fetch=ctx.models_fetch, http_client=ctx.http_client)
    fetch_fn = ctx.fetch_slot_models
    existing_ids = {row["id"] for row in listing["data"]}
    serving_upstreams = [upstream for upstream in ctx.upstreams if upstream.serves_inference]
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
    if ctx.gpu_cfg is not None:
        gpu_status = ctx.hermes_watcher.snapshot() if ctx.hermes_watcher is not None else {}
        for model_id in public_hermes_model_ids():
            profile_id = ctx.gpu_cfg.profile_for_model(model_id)
            profile_cfg = ctx.gpu_cfg.profiles.get(profile_id or "") or {}
            configured_n_ctx = profile_cfg.get("context_size")
            effective_n_ctx = (
                gpu_status.get("effective_n_ctx")
                if profile_id and gpu_status.get("loaded") == profile_id
                else None
            )
            existing = next((row for row in listing["data"] if row.get("id") == model_id), None)
            entry = existing or {
                "id": model_id,
                "object": "model",
                "created": 0,
                "owned_by": "local-model-router",
                "meta": {"kind": "alias", "maps_to_role": "chat"},
            }
            entry["capabilities"] = {
                "tools": bool(profile_cfg.get("supports_tools")),
                "vision": False,
                "json_mode": bool(profile_cfg.get("supports_json_mode")),
            }
            entry["context_size"] = effective_n_ctx
            entry.setdefault("meta", {}).update({
                "hermes_gpu": True,
                "hermes_profile": profile_id,
                "configured_n_ctx": configured_n_ctx,
                "effective_n_ctx": effective_n_ctx,
                "context_source": gpu_status.get("context_source") if effective_n_ctx else None,
                "supports_vision": False,
                "vision_note": "No mmproj is loaded in this profile",
            })
            if existing is None:
                existing_ids.add(model_id)
                listing["data"].append(entry)
    return JSONResponse(listing)


async def embeddings(ctx: RouterContext, request: Request) -> Response:
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
    requested_model = str(body.get("model") or "embedding").strip() or "embedding"
    try:
        agent = identity_from_headers(request.headers, body)
        app_id = _app_id_from(request.headers, body, agent)
        intent = _intent_from_chat_body(
            body, agent, app_id=app_id, requested_model=requested_model
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

    request_id = ctx.store.create_request(agent)
    started = time.monotonic()
    admission = None
    acquired_queue: Optional[FleetQueue] = None
    admission_lane = "local"
    tracked_slot_id: Optional[str] = None
    finished = False

    def embed_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        current = acquired_queue.snapshot() if acquired_queue is not None else {"queued": 0}
        headers = {
            "x-a0-request-id": request_id,
            "x-a0-admission-lane": admission_lane,
            "x-a0-agent-id": agent.agent_id,
            "x-a0-agent-type": agent.agent_type,
            "x-a0-fleet-request-id": request_id,
            "x-a0-fleet-queue-depth": str(current["queued"]),
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
        usage: Optional[Dict[str, Any]] = None,
    ) -> None:
        nonlocal finished, tracked_slot_id
        if finished:
            return
        finished = True
        duration_ms = int((time.monotonic() - started) * 1000)
        usage = usage or {}
        try:
            ctx.store.update_request(
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
                selected_source="local",
                admission_lane=admission_lane,
                fallback_used=getattr(decision, "fallback_used", False),
                reason_codes=getattr(decision, "reason_codes", None) or [],
                cache_status="BYPASS",
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
            )
        except Exception as exc:
            SERVICE_LOGGER.warning(
                "Embedding telemetry finalization failed (%s)",
                type(exc).__name__,
            )
        finally:
            if tracked_slot_id is not None and ctx.shared_manager is not None:
                ctx.shared_manager.finish_slot_request(tracked_slot_id)
                tracked_slot_id = None
            if acquired_queue is not None:
                await acquired_queue.release()

    try:
        decision = await ctx.intent_handler.handle(intent)
    except Exception as exc:
        await finish("failed", error_code="routing_error")
        return _openai_error(
            f"embedding routing failed: {type(exc).__name__}: {exc}",
            "routing_error",
            500,
            error_type="server_error",
            headers=embed_headers(),
        )
    if decision.no_slot_available or not decision.selected_url:
        await finish("failed", error_code="no_slot_available", decision=decision)
        return _openai_error(
            "no healthy local embedding slot is available",
            "no_slot_available",
            503,
            error_type="server_error",
            extra={"routing": decision.model_dump()},
            headers=embed_headers(),
        )

    coordinated_slot = (
        decision.selected_slot_id
        if ctx.shared_manager is not None
        and decision.selected_slot_id in getattr(ctx.shared_manager, "_slot_configs", {})
        else None
    )
    slot_epoch: Optional[int] = None
    if coordinated_slot is not None:
        operation = ctx.shared_manager.slot_operation_snapshot(coordinated_slot)
        slot_epoch = int(operation["epoch"])
        if operation["phase"] != "idle":
            await finish("rejected", error_code="gpu_swap_in_progress", decision=decision)
            return _openai_error(
                "The selected embedding slot is being changed; retry this request",
                "gpu_swap_in_progress",
                503,
                error_type="server_error",
                extra={"routing": decision.model_dump()},
                headers=embed_headers({"Retry-After": "5"}),
            )

    target_queue = ctx.admission_queues["local"]
    try:
        admission = await target_queue.acquire(agent.priority)
        acquired_queue = target_queue
        ctx.store.update_request(
            request_id,
            status="admitted",
            queued_ms=admission.queued_ms,
            app_id=app_id,
            requested_model=requested_model,
            routing_strategy=decision.routing_strategy,
            admission_lane=admission_lane,
        )
        ctx.store.record_queue_event(
            request_id=request_id,
            agent=agent,
            event_type="admitted",
            queue_depth=admission.queue_depth_at_admit,
            active_count=admission.active_at_admit,
            admission_lane=admission_lane,
        )
    except QueueFull as exc:
        await finish("rejected", error_code="queue_full", decision=decision)
        ctx.store.record_queue_event(
            request_id=request_id,
            agent=agent,
            event_type="queue_full",
            queue_depth=exc.queue_depth,
            active_count=target_queue.snapshot()["active"],
            admission_lane=admission_lane,
        )
        return _openai_error(
            "admission lane 'local' is full",
            "queue_full",
            429,
            error_type="server_error",
            extra={
                "queue": {
                    "admission_lane": admission_lane,
                    "queue_depth": exc.queue_depth,
                    "max_queue": exc.max_queue,
                    "max_active": target_queue.max_active,
                }
            },
            headers=embed_headers(),
        )
    except asyncio.CancelledError:
        await finish("failed", error_code="client_disconnected", decision=decision)
        raise

    try:
        if coordinated_slot is not None:
            lease_result = ctx.shared_manager.try_begin_slot_request(
                coordinated_slot, expected_epoch=slot_epoch
            )
            if lease_result != "ok":
                await finish("rejected", error_code=lease_result, decision=decision)
                return _openai_error(
                    "The selected embedding slot is being changed; retry this request",
                    lease_result,
                    503,
                    error_type="server_error",
                    extra={"routing": decision.model_dump()},
                    headers=embed_headers({"Retry-After": "5"}),
                )
            tracked_slot_id = coordinated_slot

        payload = {key: value for key, value in body.items() if key not in _ROUTING_ONLY_KEYS}
        payload.pop("stream", None)
        payload["model"] = _forward_model_for(body, "embedding", decision.selected_model) or "local"
        url = decision.selected_url.rstrip("/") + "/embeddings"
        headers = embed_headers({
            "x-a0-router-slot-id": decision.selected_slot_id or "",
            "x-a0-router-model": decision.selected_model or "",
            "x-a0-router-strategy": decision.routing_strategy,
        })
        async with ctx.http_client.request_session() as session:
            async with session.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=_FORWARD_TIMEOUT_SECONDS),
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    upstream_text = await response.text()
                    await finish(
                        "failed",
                        error_code="upstream_invalid_json",
                        slot_id=decision.selected_slot_id,
                        model=decision.selected_model,
                        decision=decision,
                    )
                    return _openai_error(
                        "embedding slot response was not valid JSON",
                        "upstream_invalid_json",
                        502,
                        error_type="server_error",
                        extra={"upstream_status": response.status, "upstream_body": upstream_text[:1000]},
                        headers=headers,
                    )
                usage = _dict_or_empty(data.get("usage")) if isinstance(data, dict) else {}
                await finish(
                    "completed" if response.status < 400 else "failed",
                    error_code=None if response.status < 400 else "upstream_error",
                    slot_id=decision.selected_slot_id,
                    model=decision.selected_model,
                    decision=decision,
                    usage=usage,
                )
                return JSONResponse(data, status_code=response.status, headers=headers)
    except asyncio.CancelledError:
        await finish(
            "failed",
            error_code="client_disconnected",
            slot_id=decision.selected_slot_id,
            model=decision.selected_model,
            decision=decision,
        )
        raise
    except aiohttp.ClientError as exc:
        await finish(
            "failed",
            error_code="upstream_unreachable",
            slot_id=decision.selected_slot_id,
            model=decision.selected_model,
            decision=decision,
        )
        return _openai_error(
            f"could not reach embedding slot: {exc}",
            "upstream_unreachable",
            502,
            error_type="server_error",
            headers=embed_headers(),
        )
    except TimeoutError:
        await finish(
            "failed",
            error_code="upstream_timeout",
            slot_id=decision.selected_slot_id,
            model=decision.selected_model,
            decision=decision,
        )
        return _openai_error(
            "embedding slot timed out",
            "upstream_timeout",
            504,
            error_type="server_error",
            headers=embed_headers(),
        )
    except Exception:
        await finish(
            "failed",
            error_code="upstream_error",
            slot_id=decision.selected_slot_id,
            model=decision.selected_model,
            decision=decision,
        )
        return _openai_error(
            "embedding slot request failed",
            "upstream_error",
            502,
            error_type="server_error",
            headers=embed_headers(),
        )


async def handle_chat_completions(ctx: RouterContext, request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return _openai_error("request body is not valid JSON", "invalid_json", 400)

    if not isinstance(body, dict):
        return _openai_error("request body must be a JSON object", "invalid_request_body", 400)

    if "messages" not in body:
        return _openai_error("missing required field: messages", "missing_messages", 400, param="messages")

    dedicated = getattr(request.state, "harness_connection", None)
    hermes_request = bool(dedicated and dedicated.get("harness_id") == "hermes")
    pinned_slot_id: Optional[str] = None
    try:
        agent = identity_from_headers(request.headers, body)
        requested_model = str(body.get("model") or "auto").strip() or "auto"
        if dedicated:
            app_id = dedicated["harness_id"]
            effective_model = dedicated["model"]
            slots = ctx.observer.get_slots()
            pinned_slot_id = _slot_id_for_model(slots, effective_model)
            if pinned_slot_id:
                slot = next(row for row in slots if row.get("id") == pinned_slot_id)
                body["routing"] = {
                    **{
                        key: value for key, value in _dict_or_empty(body.get("routing")).items()
                        if key in {"local_only", "privacy_mode"}
                    },
                    "preferred_slot": pinned_slot_id,
                    "role": str(slot.get("role") or "chat"),
                }
        else:
            app_id = _app_id_from(request.headers, body, agent)
            effective_model, policy_error = ctx.app_profiles.apply(app_id, body.get("model"))
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

    request_id = ctx.store.create_request(agent)
    started = time.monotonic()
    admission = None
    acquired_queue: Optional[FleetQueue] = None
    admission_lane = "unresolved"
    upstream_name: Optional[str] = None
    tracked_slot_id: Optional[str] = None
    pre_route_slot_id: Optional[str] = None
    pre_route_epoch: Optional[int] = None
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
        nonlocal finished, tracked_slot_id
        if finished:
            return
        finished = True
        duration_ms = int((time.monotonic() - started) * 1000)
        usage = usage or {}
        try:
            ctx.store.update_request(
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
        except Exception as exc:
            SERVICE_LOGGER.warning(
                "Fleet request finalization telemetry failed (%s)",
                type(exc).__name__,
            )
        finally:
            if tracked_slot_id is not None and ctx.shared_manager is not None:
                ctx.shared_manager.finish_slot_request(tracked_slot_id)
                tracked_slot_id = None
            if acquired_queue is not None:
                await acquired_queue.release()

    async def admit(lane: str) -> Optional[Response]:
        nonlocal admission_lane, admission, acquired_queue
        admission_lane = lane
        target_queue = ctx.admission_queues.get(lane)
        try:
            if target_queue is not None:
                admission = await target_queue.acquire(agent.priority)
                acquired_queue = target_queue
            ctx.store.update_request(
                request_id,
                status="admitted",
                queued_ms=admission.queued_ms if admission is not None else 0,
                app_id=app_id,
                requested_model=requested_model,
                routing_strategy=intent.routing_strategy,
                upstream_name=upstream_name,
                admission_lane=lane,
            )
            ctx.store.record_queue_event(
                request_id=request_id,
                agent=agent,
                event_type="admitted",
                queue_depth=admission.queue_depth_at_admit if admission is not None else 0,
                active_count=admission.active_at_admit if admission is not None else 0,
                admission_lane=lane,
            )
            return None
        except QueueFull as exc:
            await finish("rejected", error_code="queue_full", cache_status="BYPASS")
            ctx.store.record_queue_event(
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
        except asyncio.CancelledError:
            await finish(
                "failed",
                error_code="client_disconnected",
                cache_status="BYPASS",
            )
            raise
        except Exception:
            await finish(
                "failed",
                error_code="request_admission_failed",
                cache_status="BYPASS",
            )
            return _openai_error(
                "The request could not be admitted",
                "request_admission_failed",
                500,
                error_type="server_error",
                headers=fleet_headers(),
            )

    async def refresh_hermes_ingress(*, decision: Any = None) -> Optional[Response]:
        if not hermes_request or ctx.hermes_watcher is None:
            return None
        try:
            await ctx.hermes_watcher.refresh_desired(probe_loaded=False)
        except Exception as exc:
            SERVICE_LOGGER.warning(
                "Hermes GPU ingress refresh failed (%s); keeping the verified runtime state",
                type(exc).__name__,
            )
        retry = ctx.hermes_watcher.retry_response()
        if retry is None:
            return None
        code, retry_after = retry
        await finish(
            "rejected",
            error_code=code,
            model=effective_model,
            decision=decision,
            cache_status="BYPASS",
        )
        retry_headers = {"Retry-After": str(retry_after)} if retry_after > 0 else {}
        error_messages = {
            "gpu_swap_in_progress": "The selected GPU model is still loading; retry this request",
            "gpu_slot_stopped": "The managed GPU slot is stopped; start it before retrying",
            "gpu_leased_by_studio": (
                "The chat model is borrowed by Imperium Studio and returns when the job finishes. "
                "Retry after the given delay. Do not start the slot."
            ),
        }
        return _openai_error(
            error_messages.get(
                code,
                "The selected GPU model failed to load and requires operator action",
            ),
            code,
            503,
            error_type="server_error",
            extra={"hermes_gpu": ctx.hermes_watcher.snapshot()},
            headers=fleet_headers(retry_headers),
        )

    if hermes_request and ctx.hermes_watcher is not None:
        retry_response = await refresh_hermes_ingress()
        if retry_response is not None:
            return retry_response

        # A verified load updates only the in-memory Hermes connection. Re-read
        # it here so a request arriving after /model uses the current identity.
        try:
            current_connection = ctx.harness_profiles.resolve(
                dedicated["harness_id"], dedicated["connection"]
            )
        except (HarnessConfigError, KeyError):
            current_connection = None
        if current_connection is not None and current_connection.model != effective_model:
            effective_model = current_connection.model
            dedicated = {**dedicated, "model": effective_model}
            request.state.harness_connection = dedicated
            body["model"] = effective_model
            slots = ctx.observer.get_slots()
            pinned_slot_id = _slot_id_for_model(slots, effective_model)
            if pinned_slot_id:
                slot = next(row for row in slots if row.get("id") == pinned_slot_id)
                body["routing"] = {
                    **{
                        key: value for key, value in _dict_or_empty(body.get("routing")).items()
                        if key in {"local_only", "privacy_mode"}
                    },
                    "preferred_slot": pinned_slot_id,
                    "role": str(slot.get("role") or "chat"),
                }
            intent = _intent_from_chat_body(
                body,
                agent=agent,
                app_id=app_id,
                requested_model=requested_model,
            )

    slots = ctx.observer.get_slots()
    upstream_match = match_upstream_model(effective_model, ctx.upstreams)
    pre_route_slot_id = pinned_slot_id or _slot_id_for_model(slots, effective_model)
    if pre_route_slot_id is None and ctx.gpu_cfg is not None:
        if ctx.gpu_cfg.profile_for_model(effective_model) is not None:
            pre_route_slot_id = ctx.gpu_cfg.slot_id
    if pre_route_slot_id is None:
        preferred = body.get("routing") if isinstance(body.get("routing"), dict) else {}
        candidate = str(preferred.get("preferred_slot") or "")
        if candidate:
            pre_route_slot_id = candidate
    if (
        pre_route_slot_id is None
        and upstream_match is None
        and ctx.shared_manager is not None
        and intent.role
    ):
        role_slots = [
            str(slot.get("id") or "")
            for slot in slots
            if slot.get("enabled")
            and slot.get("role") == intent.role
            and str(slot.get("id") or "")
            in getattr(ctx.shared_manager, "_slot_configs", {})
        ]
        if len(role_slots) == 1:
            pre_route_slot_id = role_slots[0]
    if (
        ctx.shared_manager is not None
        and pre_route_slot_id in getattr(ctx.shared_manager, "_slot_configs", {})
    ):
        operation = ctx.shared_manager.slot_operation_snapshot(pre_route_slot_id)
        pre_route_epoch = int(operation["epoch"])
        if operation["phase"] != "idle":
            admission_lane = "local"
            await finish(
                "rejected",
                error_code="gpu_swap_in_progress",
                cache_status="BYPASS",
            )
            retry_after = 5
            if (
                ctx.hermes_watcher is not None
                and ctx.gpu_cfg is not None
                and pre_route_slot_id == ctx.gpu_cfg.slot_id
            ):
                retry_after = max(
                    1,
                    ctx.hermes_watcher.snapshot().get("retry_after_seconds") or 5,
                )
            return _openai_error(
                "The selected GPU model is being changed; retry this request",
                "gpu_swap_in_progress",
                503,
                error_type="server_error",
                headers=fleet_headers({"Retry-After": str(retry_after)}),
            )

    if upstream_match is not None:
        upstream_name = upstream_match[0].name
        route_reason_codes.append("pinned_harness" if dedicated else "explicit_upstream")
        if intent.local_only or intent.privacy_mode == "local_only":
            admission_lane = f"upstream:{upstream_name}"
            route_reason_codes.append("local_only_enforced")
            await finish("rejected", error_code="local_only_violation", cache_status="BYPASS")
            return _openai_error(
                "local-only requests cannot use a declared upstream",
                "local_only_violation",
                403,
                headers=fleet_headers(),
            )
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
        return await forward_to_upstream(ctx, 
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
        decision = await ctx.intent_handler.handle(intent)
    except Exception as exc:
        SERVICE_LOGGER.error("Routing decision failed (%s)", type(exc).__name__)
        await finish("failed", error_code="routing_error")
        return _openai_error(
            "routing decision failed",
            "routing_error",
            500,
            error_type="server_error",
            headers=fleet_headers(),
        )
    decision_body = decision.model_dump()
    gpu_operation = None
    if ctx.shared_manager is not None and ctx.gpu_cfg is not None:
        gpu_operation = ctx.shared_manager.slot_operation_snapshot(ctx.gpu_cfg.slot_id)
    if (
        (decision.no_slot_available or not decision.selected_url)
        and gpu_operation is not None
        and gpu_operation.get("phase") != "idle"
        and pre_route_slot_id == ctx.gpu_cfg.slot_id
    ):
        admission_lane = "local"
        await finish(
            "rejected",
            error_code="gpu_swap_in_progress",
            decision=decision,
            cache_status="BYPASS",
        )
        retry_after = 5
        if ctx.hermes_watcher is not None:
            retry_after = max(1, ctx.hermes_watcher.snapshot().get("retry_after_seconds") or 5)
        return _openai_error(
            "The GPU model is being changed; retry this request",
            "gpu_swap_in_progress",
            503,
            error_type="server_error",
            extra={
                "routing": decision_body,
                "hermes_gpu": ctx.hermes_watcher.snapshot() if ctx.hermes_watcher is not None else {},
            },
            headers=fleet_headers({"Retry-After": str(retry_after)}),
        )
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
                for upstream in ctx.upstreams
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
        return await forward_to_upstream(ctx, 
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

    slot_epoch: Optional[int] = None
    coordinated_slot = (
        decision.selected_slot_id
        if ctx.shared_manager is not None
        and decision.selected_slot_id in getattr(ctx.shared_manager, "_slot_configs", {})
        else None
    )
    if coordinated_slot is not None:
        operation = ctx.shared_manager.slot_operation_snapshot(coordinated_slot)
        slot_epoch = (
            pre_route_epoch
            if coordinated_slot == pre_route_slot_id and pre_route_epoch is not None
            else int(operation["epoch"])
        )
        if operation["phase"] != "idle":
            await finish(
                "rejected",
                error_code="gpu_swap_in_progress",
                decision=decision,
                cache_status="BYPASS",
            )
            retry_after = 5
            if ctx.hermes_watcher is not None and ctx.gpu_cfg is not None and coordinated_slot == ctx.gpu_cfg.slot_id:
                retry_after = max(1, ctx.hermes_watcher.snapshot().get("retry_after_seconds") or 5)
            return _openai_error(
                "The selected GPU model is being changed; retry this request",
                "gpu_swap_in_progress",
                503,
                error_type="server_error",
                extra={"routing": decision_body},
                headers=fleet_headers({"Retry-After": str(retry_after)}),
            )

    selected_role = next(
        (slot.get("role") for slot in slots if slot.get("id") == decision.selected_slot_id),
        None,
    )
    admission_error = await admit("local:scribe" if selected_role == "scribe" else "local")
    if admission_error is not None:
        return admission_error
    try:
        if (
            hermes_request
            and ctx.hermes_watcher is not None
            and ctx.gpu_cfg is not None
            and coordinated_slot == ctx.gpu_cfg.slot_id
        ):
            retry_response = await refresh_hermes_ingress(decision=decision)
            if retry_response is not None:
                return retry_response
        if coordinated_slot is not None:
            lease_result = ctx.shared_manager.try_begin_slot_request(
                coordinated_slot, expected_epoch=slot_epoch
            )
            if lease_result != "ok":
                await finish(
                    "rejected",
                    error_code=lease_result,
                    decision=decision,
                    cache_status="BYPASS",
                )
                return _openai_error(
                    (
                        "The selected GPU model is being changed; retry this request"
                        if lease_result == "gpu_swap_in_progress"
                        else "The selected GPU model changed while this request was waiting; retry it"
                    ),
                    lease_result,
                    503,
                    error_type="server_error",
                    extra={"routing": decision_body},
                    headers=fleet_headers(
                        {
                            "Retry-After": "5"
                            if lease_result == "gpu_swap_in_progress"
                            else "1"
                        }
                    ),
                )
            tracked_slot_id = coordinated_slot
        url = decision.selected_url.rstrip("/") + "/chat/completions"
        wants_stream = body.get("stream") is True
        forward_model = _forward_model_for(body, intent.task_type, decision.selected_model)
        default_max_tokens = (
            ctx.gpu_cfg.default_max_tokens(forward_model or decision.selected_model)
            if ctx.gpu_cfg is not None and decision.selected_slot_id == ctx.gpu_cfg.slot_id
            else None
        )
        payload = _forward_payload(
            body,
            forward_model,
            stream=wants_stream,
            default_max_tokens=default_max_tokens,
        )
        selected_context = resolve_app_hook("_context_for_slot", _context_for_slot)(
            ctx.observer.get_slots(),
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
        if ctx.prompt_cache is not None and not wants_stream and prompt_is_cacheable(payload):
            cache_lookup_key = prompt_cache_key(
                payload, resolved_model=forward_model or decision.selected_model or ""
            )
            cached = ctx.prompt_cache.get(cache_lookup_key)
            if cached is not None:
                cache_status = "HIT"
                cached_headers = router_response_headers(cache_status)
                if _response_was_truncated(cached):
                    cached_headers["x-a0-router-output-truncated"] = "true"
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
                    headers=cached_headers,
                )
            cache_status = "MISS"
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
            error_code="request_preparation_failed",
            slot_id=decision.selected_slot_id,
            model=decision.selected_model,
            decision=decision,
            cache_status="BYPASS",
        )
        return _openai_error(
            "The selected local request could not be prepared",
            "request_preparation_failed",
            500,
            error_type="server_error",
            extra={"routing": decision_body},
            headers=fleet_headers(),
        )

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
            # A stream is never buffered, so a JSON request cannot be
            # checked. Say so rather than let silence imply it was.
            if structured_output.json_output_requested(payload):
                headers["x-a0-router-structured-output"] = structured_output.UNSUPPORTED

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
                finally:
                    if not finished:
                        await finish(
                            "failed",
                            error_code="client_disconnected",
                            slot_id=decision.selected_slot_id,
                            model=decision.selected_model,
                            decision=decision,
                            cache_status="BYPASS",
                        )

            async def close_managed_stream() -> None:
                try:
                    resp.release()
                except Exception:
                    pass
                try:
                    await session.close()
                finally:
                    if not finished:
                        await finish(
                            "failed",
                            error_code="client_disconnected",
                            slot_id=decision.selected_slot_id,
                            model=decision.selected_model,
                            decision=decision,
                            cache_status="BYPASS",
                        )

            return _ManagedStreamingResponse(
                managed_stream(),
                status_code=resp.status,
                media_type="text/event-stream",
                headers=headers,
                on_close=close_managed_stream,
            )

        async with ctx.http_client.request_session() as session:
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

                async def resend_local(repair_payload: Dict[str, Any]) -> Any:
                    # Rides the admission lease this request already holds:
                    # a second acquire would deadlock at max_active=1.
                    async with session.post(
                        url,
                        json=repair_payload,
                        headers={"Content-Type": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=_FORWARD_TIMEOUT_SECONDS),
                    ) as retry_resp:
                        if retry_resp.status >= 400:
                            return None
                        return await retry_resp.json(content_type=None)

                upstream_json, structured_code = await check_structured_output(
                    payload, upstream_json, resend_local
                )
                if structured_code:
                    headers["x-a0-router-structured-output"] = structured_code

                if structured_code == structured_output.FAILED and _structured_output_enforced():
                    await finish(
                        "failed",
                        error_code=structured_output.FAILED,
                        slot_id=decision.selected_slot_id,
                        model=decision.selected_model,
                        decision=decision,
                        cache_status="BYPASS",
                        usage=_dict_or_empty(upstream_json.get("usage"))
                        if isinstance(upstream_json, dict)
                        else {},
                    )
                    return _openai_error(
                        "the model did not return valid JSON for a response_format request",
                        structured_output.FAILED,
                        422,
                        extra={"routing": decision_body},
                        headers=headers,
                    )

                if _response_was_truncated(upstream_json):
                    headers["x-a0-router-output-truncated"] = "true"
                await finish(
                    "completed",
                    slot_id=decision.selected_slot_id,
                    model=decision.selected_model,
                    decision=decision,
                    cache_status=cache_status,
                    usage=_dict_or_empty(upstream_json.get("usage")),
                )
                # A reply that failed validation is never cached: caching it
                # would serve the same bad body again as a hit, skipping this
                # check entirely.
                if (
                    cache_status == "MISS"
                    and cache_lookup_key is not None
                    and ctx.prompt_cache is not None
                    and structured_code != structured_output.FAILED
                ):
                    ctx.prompt_cache.set(cache_lookup_key, upstream_json)
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



def build_openai_routes(ctx: RouterContext) -> list[Route]:
    return [
        Route("/v1/models", ctx.protected(ctx.bind(v1_models))),
        Route("/v1/chat/completions", ctx.protected(ctx.bind(handle_chat_completions)), methods=["POST"]),
        Route("/v1/embeddings", ctx.protected(ctx.bind(embeddings)), methods=["POST"]),
    ]

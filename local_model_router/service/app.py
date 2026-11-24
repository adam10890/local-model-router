"""HTTP observation, fleet control, setup, harness, and OpenAI gateway for Imperium."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from starlette.applications import Starlette

from local_model_router import __version__ as _VERSION
from local_model_router.apps.profiles import AppProfiles
from local_model_router.diagnostics import collect_doctor_checks
from local_model_router.disclosure import (
    CONFIG_FILENAME as DISCLOSURE_CONFIG_FILENAME,
    DisclosureConfigError,
    load_policy as load_disclosure_policy,
)
from local_model_router.execution import load_execution_catalog
from local_model_router.harnesses import HarnessProfiles
from local_model_router.helpers.compute_monitor import scan_hardware
from local_model_router.setup import SetupEngine
from local_model_router.upstreams.registry import load_upstreams
from .agent_library import (
    AgentCatalog,
    AgentRunTimeout,
    AgentRunnerUnavailable,
    run_agent,
)
from .agent_orchestrator import AgentOrchestrator
from .context import (
    LEGACY_ORCHESTRATOR_SUNSET,
    _SERVICE_NAME,
    RouterContext,
    _ManagedStreamingResponse,
    _configured_api_key,
    _context_for_slot,
    _record_upstream_usage,
    _stream_upstream_response,
    legacy_orchestrator_enabled,
)
from .fleet_control import FleetControlHandler, configured_backend, fleet_control_enabled
from .fleet_manager import FleetQueue, FleetStore, _int_from_env
from .hermes_model_watcher import (
    HermesModelWatcher,
    load_hermes_gpu_config,
    pin_extra_ids,
)
from .http_client import SharedHttpClient
from .models_listing import FetchFn
from .observer import ObserverBackend
from .prompt_cache import InMemoryPromptCache, prompt_cache_enabled
from .readiness import inspect_running_llama_models
from .routes_fleet import build_fleet_routes
from .routes_harnesses import build_harness_routes
from .routes_openai import build_openai_routes
from .routes_orchestrator import build_orchestrator_routes
from .routes_setup import build_setup_routes
from .routing_intent import RoutingIntentHandler
from .swarm_gate import SwarmTerminalRegistry, load_swarm_policy

logger = logging.getLogger(__name__)

__all__ = [
    "AgentRunTimeout",
    "AgentRunnerUnavailable",
    "LEGACY_ORCHESTRATOR_SUNSET",
    "ObserverBackend",
    "RouterContext",
    "SetupEngine",
    "_ManagedStreamingResponse",
    "_SERVICE_NAME",
    "_VERSION",
    "_context_for_slot",
    "_record_upstream_usage",
    "_stream_upstream_response",
    "collect_doctor_checks",
    "create_app",
    "inspect_running_llama_models",
    "legacy_orchestrator_enabled",
    "run_agent",
    "scan_hardware",
]


def create_app(
    config_path: str | os.PathLike[str] | None = None,
    *,
    fleet_store: Optional[FleetStore] = None,
    fleet_queue: Optional[FleetQueue] = None,
    scribe_queue: Optional[FleetQueue] = None,
    upstreams_path: str | os.PathLike[str] | None = None,
    apps_path: str | os.PathLike[str] | None = None,
    harnesses_path: str | os.PathLike[str] | None = None,
    agents_path: str | os.PathLike[str] | None = None,
    orchestrator: Optional[AgentOrchestrator] = None,
    setup_home: str | os.PathLike[str] | None = None,
    setup_api_enabled: Optional[bool] = None,
    models_fetch: Optional[FetchFn] = None,
) -> Starlette:
    """Return a configured Starlette app. Safe to call multiple times (no side-effects)."""
    observer = ObserverBackend(config_path)
    api_key = _configured_api_key()
    config_writes_requested = os.environ.get("A0_LMM_ROUTER_ENABLE_CONFIG_WRITES") == "1"
    config_writes_enabled = config_writes_requested and bool(api_key)
    store = fleet_store or FleetStore()
    queue = fleet_queue or FleetQueue()
    prompt_cache = InMemoryPromptCache() if prompt_cache_enabled() else None
    setup_engine = SetupEngine(home=setup_home, config_path=observer.config_path)
    setup_token = secrets.token_urlsafe(24)
    setup_api_active = {
        "value": bool(setup_api_enabled)
        if setup_api_enabled is not None
        else not Path(observer.config_path).is_file()
    }

    conf_dir = Path(observer.config_path).resolve().parent
    gpu_cfg = load_hermes_gpu_config(conf_dir / "hermes_gpu_profiles.yaml", observer.config_path)
    gpu_pin_ids = pin_extra_ids(gpu_cfg.profiles) if gpu_cfg else set()
    watcher_disabled = os.environ.get("A0_LMM_ROUTER_HERMES_WATCHER", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }
    control_enabled = fleet_control_enabled()
    fleet_backend = configured_backend(observer.config_path)
    supports_start_stop = fleet_backend in {"auto", "docker", "subprocess"}
    shared_manager = None
    if control_enabled and supports_start_stop:
        from local_model_router.helpers.llama_cpp_manager import BackendManager

        try:
            shared_manager = BackendManager.get_instance(observer.config_path)
        except ImportError:
            shared_manager = None
        if shared_manager is not None:
            fleet_backend = shared_manager.backend_type
            supports_start_stop = fleet_backend in {"docker", "subprocess"}
            if shared_manager.backend_type == "subprocess":
                shared_manager.configure_process_ownership(setup_engine.process_path)
                if gpu_cfg is not None:
                    shared_manager.adopt_slot(gpu_cfg.slot_id)
    fleet_control = FleetControlHandler(observer.config_path, manager=shared_manager)
    upstreams = load_upstreams(upstreams_path or conf_dir / "upstreams.yaml")
    try:
        disclosure_policy = load_disclosure_policy(conf_dir / DISCLOSURE_CONFIG_FILENAME)
    except DisclosureConfigError as exc:
        logger.warning("Disclosure rules override rejected, using packaged rules: %s", exc)
        disclosure_policy = load_disclosure_policy(None)

    admission_queues = {
        "local": queue,
        "local:scribe": scribe_queue or FleetQueue(
            _int_from_env("A0_FLEET_SCRIBE_MAX_ACTIVE", 1, minimum=1),
            _int_from_env("A0_FLEET_SCRIBE_MAX_QUEUE", 4, minimum=0),
        ),
    }
    admission_queues.update({
        f"upstream:{upstream.name}": FleetQueue(upstream.max_active, upstream.max_queue)
        for upstream in upstreams
        if upstream.max_active is not None and not upstream.config_error
    })
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
    agent_orchestrator = orchestrator or (
        AgentOrchestrator(
            repo_root=str(Path(__file__).resolve().parents[2]),
            bundles_path=str(conf_dir / "agent_orchestrator.yaml"),
        )
        if legacy_orchestrator_enabled()
        else None
    )
    http_client = SharedHttpClient()
    health_checker = getattr(observer, "_health_checker", None)
    if health_checker is not None and getattr(health_checker, "_http_client", None) is None:
        health_checker._http_client = http_client

    ctx = RouterContext(
        observer=observer,
        api_key=api_key,
        config_writes_requested=config_writes_requested,
        config_writes_enabled=config_writes_enabled,
        store=store,
        queue=queue,
        prompt_cache=prompt_cache,
        compute_lock=asyncio.Lock(),
        setup_engine=setup_engine,
        setup_token=setup_token,
        setup_api_active=setup_api_active,
        conf_dir=conf_dir,
        gpu_cfg=gpu_cfg,
        gpu_pin_ids=gpu_pin_ids,
        control_enabled=control_enabled,
        fleet_backend=fleet_backend,
        supports_start_stop=supports_start_stop,
        shared_manager=shared_manager,
        fleet_control=fleet_control,
        upstreams=upstreams,
        disclosure_policy=disclosure_policy,
        admission_queues=admission_queues,
        intent_handler=None,
        app_profiles=app_profiles,
        agent_catalog=agent_catalog,
        harness_profiles=harness_profiles,
        hermes_watcher=None,
        watcher_task_ref={"task": None},
        harness_activity={},
        agent_orchestrator=agent_orchestrator,
        swarm_policy=None,
        swarm_registry=None,
        execution_catalog=load_execution_catalog(conf_dir),
        http_client=http_client,
        models_fetch=models_fetch,
    )
    ctx.intent_handler = RoutingIntentHandler(
        observer,
        upstream_rows_fn=lambda: [upstream.describe() for upstream in ctx.upstreams],
        budget_status_fn=ctx.upstream_budget_status,
        evaluation_snapshot_fn=lambda: ctx.store.latest_model_snapshot("model_evaluation"),
        manager_factory=(lambda: shared_manager) if shared_manager is not None else None,
    )
    hermes_watcher = (
        HermesModelWatcher(
            cfg=gpu_cfg,
            pin=ctx.pin_hermes_gpu,
            publish_fn=ctx.publish_hermes_gpu,
            clear_fn=ctx.clear_hermes_gpu,
            manager_factory=lambda: shared_manager,
        )
        if (
            gpu_cfg is not None
            and shared_manager is not None
            and shared_manager.backend_type == "subprocess"
            and not watcher_disabled
        )
        else None
    )
    if hermes_watcher is not None and not hermes_watcher.prepare_manager():
        logger.warning("Hermes GPU watcher disabled (managed slot unavailable)")
        hermes_watcher = None
    ctx.hermes_watcher = hermes_watcher
    swarm_policy = load_swarm_policy(conf_dir / "swarm.yaml")
    ctx.swarm_policy = swarm_policy
    ctx.swarm_registry = SwarmTerminalRegistry(swarm_policy, ctx.swarm_capacity)

    routes = [
        *build_setup_routes(ctx),
        *build_fleet_routes(ctx),
        *build_orchestrator_routes(ctx),
        *build_harness_routes(ctx),
        *build_openai_routes(ctx),
    ]

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        swarm_sweep = None
        if swarm_policy.enabled:
            swarm_sweep = asyncio.create_task(ctx.swarm_registry.sweep_forever())
        task = None
        if hermes_watcher is not None:
            task = asyncio.create_task(hermes_watcher.run())
            ctx.watcher_task_ref["task"] = task
        try:
            yield
        finally:
            if swarm_sweep is not None:
                swarm_sweep.cancel()
                try:
                    await swarm_sweep
                except asyncio.CancelledError:
                    pass
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                ctx.watcher_task_ref["task"] = None
            if hermes_watcher is not None:
                await hermes_watcher.stop()
            await http_client.close()

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.context = ctx
    app.state.setup_token = setup_token
    app.state.setup_engine = setup_engine
    app.state.observer = observer
    app.state.hermes_watcher = hermes_watcher
    app.state.backend_manager = shared_manager
    return app

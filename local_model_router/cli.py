"""Command-line interface for local-model-router.

    python -m local_model_router serve          # start the router (default)
    python -m local_model_router doctor         # environment + fleet checks
    python -m local_model_router list-models    # aliases + live slot models
    python -m local_model_router test-route     # dry-run a routing decision
    python -m local_model_router config-check   # validate the fleet YAML
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

PROBE_TIMEOUT_SECONDS = 3.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-model-router",
        description="Local-first model router: one OpenAI-compatible gateway for a local llama.cpp fleet.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="start the router HTTP service (default)")
    sub.add_parser("mcp", help="start the MCP server (Streamable HTTP, requires the 'mcp' extra)")
    doctor = sub.add_parser("doctor", help="check python, config, dependencies, and slot reachability")
    doctor.add_argument("--json", action="store_true", help="print structured checks as JSON")
    sub.add_parser("list-models", help="print router aliases and live slot models")
    sub.add_parser("config-check", help="parse and sanity-check the fleet config")

    route = sub.add_parser("test-route", help="dry-run a routing decision")
    route.add_argument("--role", default=None, help="explicit fleet role (chat|utility|embed|scribe)")
    route.add_argument("--task-type", default="chat", help="task type for auto-routing")
    route.add_argument("--model", default=None, help="model alias to resolve (auto, fast, coder, ...)")

    evaluate = sub.add_parser("evaluate-models", help="benchmark reachable local models and save ranking hints")
    evaluate.add_argument(
        "--base-url",
        default=os.environ.get("A0_LMM_ROUTER_BASE_URL")
        or f"http://127.0.0.1:{os.environ.get('OBSERVER_PORT', '9000')}",
        help="running router base URL",
    )
    evaluate.add_argument("--force", action="store_true", help="ignore unchanged model fingerprints")

    setup = sub.add_parser("setup", help="open first-run setup or inspect managed runtime state")
    setup.add_argument("--status", action="store_true", help="print current setup state as JSON")
    setup.add_argument("--repair", action="store_true", help="inspect setup and repair it when combined with --yes")
    setup.add_argument("--plan", help="apply a reviewed setup plan from a JSON file")
    setup.add_argument("--yes", action="store_true", help="confirm downloads and configuration writes")
    setup.add_argument("--start-runtime", action="store_true", help="start the configured managed llama.cpp server")
    setup.add_argument("--stop-runtime", action="store_true", help="stop the managed llama.cpp server")
    setup.add_argument("--terminal", action="store_true", help="show llama.cpp in its own terminal window")

    update = sub.add_parser("update", help="check or install the latest stable managed llama.cpp runtime")
    update.add_argument("--check", action="store_true", help="check without installing")
    update.add_argument("--yes", action="store_true", help="confirm the runtime download and activation")
    sub.add_parser("rollback", help="switch back to the previous managed llama.cpp runtime")

    return parser


def _resolve_config() -> str:
    from local_model_router.helpers.conf_resolver import resolve_conf_path

    return resolve_conf_path(__file__)


def _probe(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT_SECONDS) as resp:  # noqa: S310
            return 200 <= resp.status < 500
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def cmd_serve(_args: argparse.Namespace) -> int:
    from local_model_router.service.__main__ import main as serve_main

    serve_main()
    return 0


def cmd_mcp(_args: argparse.Namespace) -> int:
    try:
        from local_model_router.mcp.server import main as mcp_main
    except ImportError as exc:
        print(f"MCP support is not installed: {exc}")
        print('Install it with: pip install -e ".[mcp]"')
        return 1
    mcp_main()
    return 0


def cmd_config_check(_args: argparse.Namespace) -> int:
    import yaml
    from pathlib import Path

    from local_model_router.upstreams.registry import load_upstreams

    config_path = _resolve_config()
    print(f"config: {config_path}")
    if not os.path.exists(config_path):
        print("FAIL: config file not found")
        return 1
    try:
        with open(config_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        print(f"FAIL: YAML parse error: {exc}")
        return 1
    if not isinstance(data, dict):
        print("FAIL: top level of config must be a mapping")
        return 1

    slots = data.get("active_slots") or []
    enabled = [s for s in slots if isinstance(s, dict) and s.get("enabled", True)]
    print(f"OK: parsed; {len(slots)} slot(s) defined, {len(enabled)} enabled")
    for slot in enabled:
        slot_id = slot.get("id") or f"slot_{slot.get('port', '?')}"
        print(f"  - {slot_id}: role={slot.get('role', '?')} port={slot.get('port', '?')} router_mode={bool(slot.get('router_mode'))}")
    if not enabled:
        print("WARN: no enabled slots — the router will have nothing to route to")
    upstream_errors = [
        upstream for upstream in load_upstreams(Path(config_path).resolve().parent / "upstreams.yaml")
        if upstream.config_error
    ]
    for upstream in upstream_errors:
        print(f"FAIL: upstream {upstream.name}: {upstream.config_error}")
    return 1 if upstream_errors else 0


def cmd_list_models(_args: argparse.Namespace) -> int:
    from local_model_router.service.models_listing import list_models
    from local_model_router.service.observer import ObserverBackend

    observer = ObserverBackend(_resolve_config())
    listing = asyncio.run(list_models(observer))
    for row in listing.get("data", []):
        meta = row.get("meta") or {}
        if meta.get("kind") == "alias":
            live = meta.get("live") or {}
            suffix = ""
            if live:
                suffix = f"  [live: slot={live.get('slot_id')} n_ctx={live.get('n_ctx') or '?'}]"
            print(f"alias  {row['id']:<14} -> role {meta.get('maps_to_role')}{suffix}")
        else:
            n_ctx = meta.get("n_ctx") or "?"
            print(f"model  {row['id']:<40} slot={meta.get('slot_id')} n_ctx={n_ctx}")
    return 0


def cmd_test_route(args: argparse.Namespace) -> int:
    from local_model_router.routing.aliases import resolve_alias
    from local_model_router.service.observer import ObserverBackend
    from local_model_router.service.routing_intent import (
        RoutingIntentHandler,
        RoutingIntentRequest,
    )

    role: Optional[str] = args.role
    if role is None and args.model:
        resolution = resolve_alias(args.model, task_type=args.task_type)
        role = resolution.role
        print(f"alias '{args.model}' -> role {role} (recognized={resolution.recognized})")

    from pathlib import Path

    from local_model_router.upstreams.registry import load_upstreams

    config_path = _resolve_config()
    observer = ObserverBackend(config_path)
    upstreams = load_upstreams(Path(config_path).resolve().parent / "upstreams.yaml")
    handler = RoutingIntentHandler(
        observer,
        upstream_rows_fn=lambda: [upstream.describe() for upstream in upstreams],
    )
    intent = RoutingIntentRequest(
        agent_id="cli",
        agent_type="custom",
        role=role,
        task_type=args.task_type,
    )
    decision = asyncio.run(handler.handle(intent))
    print(json.dumps(decision.model_dump(), indent=2))
    return 0 if not decision.no_slot_available else 2


def cmd_evaluate_models(args: argparse.Namespace) -> int:
    from local_model_router.evaluation import evaluate_models, http_requester
    from local_model_router.service.fleet_manager import FleetStore

    try:
        payload = evaluate_models(
            http_requester(args.base_url, os.environ.get("A0_LMM_ROUTER_API_KEY", "")),
            FleetStore(),
            force=args.force,
        )
    except RuntimeError as exc:
        print(json.dumps({"error": "evaluation_failed", "detail": str(exc)}, indent=2))
        return 2
    print(json.dumps(payload, indent=2))
    return 0 if payload["models"] else 2


def cmd_doctor(args: argparse.Namespace) -> int:
    failures = 0
    checks: list[dict[str, Any]] = []

    def check(label: str, ok: bool, detail: str = "", remediation: str = "") -> None:
        nonlocal failures
        status = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        checks.append(
            {
                "code": label.lower().replace(" ", "_").replace(":", ""),
                "status": "pass" if ok else "fail",
                "severity": "info" if ok else "blocking",
                "label": label,
                "detail": detail,
                "remediation": remediation or None,
            }
        )
        if args.json:
            return
        suffix = f" — {detail}" if detail else ""
        print(f"[{status}] {label}{suffix}")

    check(
        "python >= 3.10",
        sys.version_info >= (3, 10),
        f"running {sys.version_info.major}.{sys.version_info.minor}",
    )

    dependencies = {
        "aiohttp": ("aiohttp", "ClientSession"),
        "pydantic": ("pydantic", "BaseModel"),
        "starlette": ("starlette.applications", "Starlette"),
        "uvicorn": ("uvicorn", "run"),
        "yaml": ("yaml", "safe_load"),
    }
    for name, (module_name, symbol) in dependencies.items():
        capability = f"{module_name}.{symbol}"
        try:
            module = importlib.import_module(module_name)
            if not callable(getattr(module, symbol, None)):
                raise AttributeError(symbol)
            check(f"dependency: {name}", True, capability)
        except (ImportError, AttributeError):
            check(
                f"dependency: {name}",
                False,
                f"required capability unavailable: {capability}",
                f"Reinstall {name} in the Imperium Python environment",
            )

    config_path = _resolve_config()
    config_ok = os.path.exists(config_path)
    check("config file exists", config_ok, config_path)

    slots: list[dict[str, Any]] = []
    if config_ok:
        try:
            from local_model_router.service.observer import ObserverBackend

            slots = ObserverBackend(config_path).get_slots()
            check("config parses", True, f"{len(slots)} slot(s)")
        except Exception as exc:
            check("config parses", False, f"{type(exc).__name__}: {exc}")

    reachable = 0
    for slot in slots:
        if not slot.get("enabled") or not slot.get("base_url"):
            continue
        url = str(slot["base_url"]).rstrip("/") + "/models"
        ok = _probe(url)
        reachable += 1 if ok else 0
        check(f"slot reachable: {slot.get('id')}", ok, url, "Start the configured model server")
    if slots and reachable == 0:
        if not args.json:
            print("       hint: is the llama.cpp fleet running? The router routes to it; it does not start it.")

    if args.json:
        print(json.dumps({"ok": failures == 0, "checks": checks}, indent=2))
    else:
        print(f"\n{'all checks passed' if failures == 0 else f'{failures} check(s) failed'}")
    return 0 if failures == 0 else 1


def _setup_engine():
    from local_model_router.setup import SetupEngine

    return SetupEngine(config_path=_resolve_config())


def cmd_setup(args: argparse.Namespace) -> int:
    from pathlib import Path

    from local_model_router.setup import SetupError

    engine = _setup_engine()
    try:
        if args.status:
            print(json.dumps(engine.state(), indent=2))
            return 0
        if args.repair:
            result = engine.repair(confirm=bool(args.yes))
            print(json.dumps(result, indent=2))
            return 0 if result.get("ok") else 1
        if args.start_runtime:
            print(json.dumps(engine.start_managed(visible_terminal=args.terminal), indent=2))
            return 0
        if args.stop_runtime:
            print(json.dumps(engine.stop_managed(), indent=2))
            return 0
        if args.plan:
            payload = json.loads(Path(args.plan).read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict):
                raise SetupError("invalid_plan", "The setup plan must be a JSON object")
            payload["confirm_download"] = bool(args.yes)
            payload["confirm_write"] = bool(args.yes)
            print(json.dumps(engine.apply(payload), indent=2))
            return 0

        import threading
        import time
        import webbrowser

        import uvicorn

        from local_model_router.service.app import create_app

        host = "127.0.0.1"
        port = int(os.environ.get("OBSERVER_PORT", "9000"))
        url = f"http://{host}:{port}/ui#/setup"

        def open_wizard() -> None:
            for _ in range(60):
                if _probe(f"http://{host}:{port}/health"):
                    webbrowser.open(url)
                    return
                time.sleep(0.25)

        threading.Thread(target=open_wizard, daemon=True).start()
        uvicorn.run(
            create_app(
                engine.config_path,
                setup_home=str(engine.home),
                setup_api_enabled=True,
            ),
            host=host,
            port=port,
        )
        return 0
    except (SetupError, OSError, ValueError, json.JSONDecodeError) as exc:
        code = getattr(exc, "code", "setup_failed")
        payload = exc.payload() if isinstance(exc, SetupError) else {"error": code, "detail": str(exc)}
        print(json.dumps(payload, indent=2))
        return 1


def cmd_update(args: argparse.Namespace) -> int:
    from local_model_router.setup import SetupError

    engine = _setup_engine()
    try:
        status = engine.update_status()
        if args.check or not status["update_available"]:
            print(json.dumps(status, indent=2))
            return 0
        if not args.yes:
            print(json.dumps({**status, "confirmation_required": True, "next": "imperium update --yes"}, indent=2))
            return 2
        runtime = engine._managed_runtime()
        if not runtime:
            raise SetupError("runtime_missing", "Install the recommended runtime before switching to latest")
        result = engine.install_runtime(str(runtime["backend"]), channel="latest")
        print(json.dumps({"ok": True, "runtime": result}, indent=2))
        return 0
    except SetupError as exc:
        print(json.dumps(exc.payload(), indent=2))
        return 1


def cmd_rollback(_args: argparse.Namespace) -> int:
    from local_model_router.setup import SetupError

    try:
        print(json.dumps({"ok": True, "runtime": _setup_engine().rollback()}, indent=2))
        return 0
    except SetupError as exc:
        print(json.dumps(exc.payload(), indent=2))
        return 1


_COMMANDS = {
    "serve": cmd_serve,
    "mcp": cmd_mcp,
    "doctor": cmd_doctor,
    "list-models": cmd_list_models,
    "test-route": cmd_test_route,
    "evaluate-models": cmd_evaluate_models,
    "config-check": cmd_config_check,
    "setup": cmd_setup,
    "update": cmd_update,
    "rollback": cmd_rollback,
}


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "serve"
    return _COMMANDS[command](args)


if __name__ == "__main__":
    raise SystemExit(main())

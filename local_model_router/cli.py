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
    sub.add_parser("doctor", help="check python, config, dependencies, and slot reachability")
    sub.add_parser("list-models", help="print router aliases and live slot models")
    sub.add_parser("config-check", help="parse and sanity-check the fleet config")

    route = sub.add_parser("test-route", help="dry-run a routing decision")
    route.add_argument("--role", default=None, help="explicit fleet role (chat|utility|embed|scribe)")
    route.add_argument("--task-type", default="chat", help="task type for auto-routing")
    route.add_argument("--model", default=None, help="model alias to resolve (auto, fast, coder, ...)")

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


def cmd_config_check(_args: argparse.Namespace) -> int:
    import yaml

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
    return 0


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

    observer = ObserverBackend(_resolve_config())
    handler = RoutingIntentHandler(observer)
    intent = RoutingIntentRequest(
        agent_id="cli",
        agent_type="custom",
        role=role,
        task_type=args.task_type,
    )
    decision = asyncio.run(handler.handle(intent))
    print(json.dumps(decision.model_dump(), indent=2))
    return 0 if not decision.no_slot_available else 2


def cmd_doctor(_args: argparse.Namespace) -> int:
    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        status = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        suffix = f" — {detail}" if detail else ""
        print(f"[{status}] {label}{suffix}")

    check(
        "python >= 3.10",
        sys.version_info >= (3, 10),
        f"running {sys.version_info.major}.{sys.version_info.minor}",
    )

    for module in ("aiohttp", "pydantic", "starlette", "uvicorn", "yaml"):
        try:
            __import__(module)
            check(f"dependency: {module}", True)
        except ImportError as exc:
            check(f"dependency: {module}", False, str(exc))

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
        check(f"slot reachable: {slot.get('id')}", ok, url)
    if slots and reachable == 0:
        print("       hint: is the llama.cpp fleet running? The router routes to it; it does not start it.")

    print(f"\n{'all checks passed' if failures == 0 else f'{failures} check(s) failed'}")
    return 0 if failures == 0 else 1


_COMMANDS = {
    "serve": cmd_serve,
    "doctor": cmd_doctor,
    "list-models": cmd_list_models,
    "test-route": cmd_test_route,
    "config-check": cmd_config_check,
}


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "serve"
    return _COMMANDS[command](args)


if __name__ == "__main__":
    raise SystemExit(main())

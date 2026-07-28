#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


BASE_URL = os.environ.get("IMPERIUM_BASE_URL", "http://127.0.0.1:9000").rstrip("/")
API_KEY = os.environ.get("IMPERIUM_API_KEY", "")


def call(method: str, path: str, payload: object = None) -> object:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"Imperium returned HTTP {exc.code}: {detail}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes client for Imperium work pages")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("payload", type=Path)
    commands.add_parser("list")
    get = commands.add_parser("get")
    get.add_argument("plan_id")
    ticket = commands.add_parser("ticket")
    ticket.add_argument("ticket_id")
    args = parser.parse_args()

    if args.command == "create":
        result = call("POST", "/orchestrator/plans", json.loads(args.payload.read_text(encoding="utf-8")))
    elif args.command == "list":
        result = call("GET", "/orchestrator/plans")
    elif args.command == "get":
        result = call("GET", f"/orchestrator/plans/{urllib.parse.quote(args.plan_id, safe='')}")
    else:
        result = call("GET", f"/orchestrator/tickets/{urllib.parse.quote(args.ticket_id, safe='')}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)

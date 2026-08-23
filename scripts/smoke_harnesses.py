#!/usr/bin/env python3
"""Run sanitized live checks against pinned harness connections."""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote


def _request(method, url, api_key, payload=None, timeout=180, stream=False):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = json.dumps(payload).encode() if payload is not None else None
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data, headers=headers, method=method),
            timeout=timeout,
        ) as response:
            if stream:
                body = response.read()
                if not body:
                    raise RuntimeError("empty_stream")
                return {"bytes": len(body)}
            return json.load(response)
    except urllib.error.HTTPError as exc:
        exc.read()
        raise RuntimeError(f"http_{exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("request_failed") from exc


def _connection_url(base_url: str, harness_id: str, connection_name: str) -> str:
    hid = quote(harness_id, safe="")
    if connection_name == "default":
        return f"{base_url}/harnesses/{hid}/v1"
    return f"{base_url}/harnesses/{hid}/{quote(connection_name, safe='')}/v1"


def _chat_ok(payload: object) -> bool:
    return isinstance(payload, dict) and bool(payload.get("choices"))


def _tool_ok(payload: object, name: str = "imperium_ping") -> bool:
    if not isinstance(payload, dict):
        return False
    choices = payload.get("choices") or []
    message = (choices[0].get("message") or {}) if choices and isinstance(choices[0], dict) else {}
    return any(
        isinstance(call, dict) and (call.get("function") or {}).get("name") == name
        for call in message.get("tool_calls") or []
    )


def _write_report(path: str | Path | None, report: dict) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


def smoke(
    base_url,
    api_key="",
    *,
    check_stream=True,
    check_tools=False,
    harness_ids: set[str] | None = None,
    timeout: int = 180,
    max_tokens: int = 256,
    json_output: str | Path | None = None,
):
    base_url = base_url.rstrip("/")
    report = {
        "schema_version": 1,
        "kind": "harness_smoke",
        "ok": False,
        "required_harnesses": sorted(harness_ids or []),
        "connections": [],
    }
    try:
        manifest = _request("GET", f"{base_url}/harnesses", api_key, timeout=timeout)
        harnesses = manifest.get("harnesses") if isinstance(manifest, dict) else None
        if not isinstance(harnesses, list):
            raise RuntimeError("invalid_harness_manifest")
        available = {str(row.get("harness_id") or "") for row in harnesses}
        missing = sorted((harness_ids or set()) - available)
        if missing:
            report["missing_harnesses"] = missing
            raise RuntimeError("required_harness_missing")

        for harness in harnesses:
            harness_id = str(harness.get("harness_id") or "")
            if harness_ids is not None and harness_id not in harness_ids:
                continue
            for connection in harness.get("connections") or []:
                name = str(connection.get("name") or "")
                label = f"{harness_id}/{name}"
                row = {"harness": harness_id, "connection": name, "checks": {}}
                report["connections"].append(row)
                url = _connection_url(base_url, harness_id, name)
                _request("GET", f"{url}/models", api_key, timeout=timeout)
                row["checks"]["models"] = "pass"
                chat = _request(
                    "POST",
                    f"{url}/chat/completions",
                    api_key,
                    {
                        "model": "local",
                        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                        "max_tokens": max_tokens,
                        "temperature": 0,
                    },
                    timeout=timeout,
                )
                if not _chat_ok(chat):
                    raise RuntimeError("invalid_chat_response")
                row["checks"]["chat"] = "pass"
                if check_stream:
                    _request(
                        "POST",
                        f"{url}/chat/completions",
                        api_key,
                        {
                            "model": "local",
                            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                            "max_tokens": max_tokens,
                            "temperature": 0,
                            "stream": True,
                        },
                        timeout=timeout,
                        stream=True,
                    )
                    row["checks"]["stream"] = "pass"
                if check_tools:
                    tools = _request(
                        "POST",
                        f"{url}/chat/completions",
                        api_key,
                        {
                            "model": "local",
                            "messages": [{"role": "user", "content": "Call imperium_ping now."}],
                            "tools": [{
                                "type": "function",
                                "function": {
                                    "name": "imperium_ping",
                                    "description": "Return a smoke verification ping",
                                    "parameters": {
                                        "type": "object",
                                        "properties": {},
                                        "additionalProperties": False,
                                    },
                                },
                            }],
                            "tool_choice": "required",
                            "chat_template_kwargs": {"enable_thinking": False},
                            "max_tokens": max_tokens,
                            "temperature": 0,
                        },
                        timeout=timeout,
                    )
                    if not _tool_ok(tools):
                        raise RuntimeError("tool_call_not_returned")
                    row["checks"]["tools"] = "pass"
                print(f"[OK] {label}")
        if not report["connections"]:
            raise RuntimeError("no_harness_connections_matched")
        report["ok"] = True
        _write_report(json_output, report)
        print(f"Harness smoke complete: {len(report['connections'])} connection(s).")
        return report
    except RuntimeError as exc:
        report["error_code"] = str(exc)
        _write_report(json_output, report)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:9000")
    parser.add_argument("--api-key", default=os.environ.get("A0_LMM_ROUTER_API_KEY", ""))
    parser.add_argument("--no-stream", action="store_true", help="skip streaming completion outside RC")
    parser.add_argument("--tools", action="store_true", help="require an actual imperium_ping tool call")
    parser.add_argument("--rc", action="store_true", help="require stream, tools, and explicit harness filters")
    parser.add_argument("--json-output", help="write a sanitized machine-readable result")
    parser.add_argument("--harness", action="append", default=[], help="required harness id (repeatable)")
    parser.add_argument("--timeout", type=int, default=180, help="per-request timeout seconds")
    parser.add_argument("--max-tokens", type=int, default=256, help="completion budget")
    args = parser.parse_args()
    if args.rc and (args.no_stream or not args.harness):
        parser.error("--rc requires at least one --harness and does not allow --no-stream")
    try:
        smoke(
            args.base_url,
            args.api_key,
            check_stream=not args.no_stream,
            check_tools=args.tools or args.rc,
            harness_ids=set(args.harness) or None,
            timeout=args.timeout,
            max_tokens=args.max_tokens,
            json_output=args.json_output,
        )
    except RuntimeError as exc:
        print(f"[FAIL] harness smoke: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

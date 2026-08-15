#!/usr/bin/env python3
"""Verify configured harnesses through their dedicated router paths.

Checks GET /models, a short chat completion, and (unless --no-stream) one
streaming completion per connection. Optional --tools sends a no-op tools
payload so the pin path accepts tool-capable requests without requiring the
model to call a tool.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
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
                # Drain SSE/chunked body; success is non-empty upstream bytes.
                body = response.read()
                if not body:
                    raise RuntimeError(f"{method} {url} -> empty stream body")
                return {"bytes": len(body)}
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"{method} {url} -> HTTP {exc.code}: {detail}") from exc


def _connection_url(base_url: str, harness_id: str, connection_name: str) -> str:
    """Match harness setup manifests: bare /v1 when the only connection is default."""
    hid = quote(harness_id, safe="")
    if connection_name == "default":
        return f"{base_url}/harnesses/{hid}/v1"
    return f"{base_url}/harnesses/{hid}/{quote(connection_name, safe='')}/v1"


def smoke(
    base_url,
    api_key="",
    *,
    check_stream=True,
    check_tools=False,
    harness_ids: set[str] | None = None,
    timeout: int = 180,
    max_tokens: int = 256,
):
    base_url = base_url.rstrip("/")
    manifest = _request("GET", f"{base_url}/harnesses", api_key, timeout=timeout)
    harnesses = manifest.get("harnesses") if isinstance(manifest, dict) else None
    if not isinstance(harnesses, list):
        raise RuntimeError("GET /harnesses returned no harness list")

    checked = 0
    for harness in harnesses:
        harness_id = str(harness.get("harness_id") or "")
        if harness_ids is not None and harness_id not in harness_ids:
            continue
        for connection in harness.get("connections") or []:
            name = str(connection.get("name") or "")
            label = f"{harness_id}/{name}"
            url = _connection_url(base_url, harness_id, name)
            _request("GET", f"{url}/models", api_key, timeout=timeout)
            chat_body = {
                "model": "local",
                "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                "max_tokens": max_tokens,
                "temperature": 0,
            }
            if check_tools:
                chat_body["tools"] = [
                    {
                        "type": "function",
                        "function": {
                            "name": "noop",
                            "description": "smoke no-op",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ]
            _request("POST", f"{url}/chat/completions", api_key, chat_body, timeout=timeout)
            if check_stream:
                stream_body = {
                    "model": "local",
                    "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                    "max_tokens": max_tokens,
                    "temperature": 0,
                    "stream": True,
                }
                _request(
                    "POST",
                    f"{url}/chat/completions",
                    api_key,
                    stream_body,
                    timeout=timeout,
                    stream=True,
                )
            checked += 1
            print(f"[OK] {label}")
    if checked == 0:
        raise RuntimeError("no harness connections matched the smoke filter")
    print(f"Harness smoke complete: {checked} connection(s).")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:9000")
    parser.add_argument("--api-key", default=os.environ.get("A0_LMM_ROUTER_API_KEY", ""))
    parser.add_argument("--no-stream", action="store_true", help="skip streaming completion")
    parser.add_argument(
        "--tools",
        action="store_true",
        help="include a no-op tools array on the non-stream chat request",
    )
    parser.add_argument(
        "--harness",
        action="append",
        default=[],
        help="only smoke this harness id (repeatable); default: all",
    )
    parser.add_argument("--timeout", type=int, default=180, help="per-request timeout seconds")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=256,
        help="completion budget (raise for thinking models)",
    )
    args = parser.parse_args()
    smoke(
        args.base_url,
        args.api_key,
        check_stream=not args.no_stream,
        check_tools=args.tools,
        harness_ids=set(args.harness) or None,
        timeout=args.timeout,
        max_tokens=args.max_tokens,
    )


if __name__ == "__main__":
    main()

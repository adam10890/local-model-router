#!/usr/bin/env python3
"""Verify every configured harness through its dedicated router path.

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


def _request(method, url, api_key, payload=None, timeout=120, stream=False):
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


def smoke(base_url, api_key="", *, check_stream=True, check_tools=False):
    base_url = base_url.rstrip("/")
    manifest = _request("GET", f"{base_url}/harnesses", api_key)
    harnesses = manifest.get("harnesses") if isinstance(manifest, dict) else None
    if not isinstance(harnesses, list):
        raise RuntimeError("GET /harnesses returned no harness list")

    checked = 0
    for harness in harnesses:
        harness_id = quote(str(harness.get("harness_id") or ""), safe="")
        for connection in harness.get("connections") or []:
            name = quote(str(connection.get("name") or ""), safe="")
            label = f"{harness_id}/{name}"
            url = f"{base_url}/harnesses/{harness_id}/{name}/v1"
            _request("GET", f"{url}/models", api_key)
            chat_body = {
                "model": "local",
                "messages": [{"role": "user", "content": "Reply OK"}],
                "max_tokens": 1,
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
            _request("POST", f"{url}/chat/completions", api_key, chat_body)
            if check_stream:
                stream_body = {
                    "model": "local",
                    "messages": [{"role": "user", "content": "Reply OK"}],
                    "max_tokens": 1,
                    "temperature": 0,
                    "stream": True,
                }
                _request(
                    "POST",
                    f"{url}/chat/completions",
                    api_key,
                    stream_body,
                    stream=True,
                )
            checked += 1
            print(f"[OK] {label}")
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
    args = parser.parse_args()
    smoke(
        args.base_url,
        args.api_key,
        check_stream=not args.no_stream,
        check_tools=args.tools,
    )


if __name__ == "__main__":
    main()

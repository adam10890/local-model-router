#!/usr/bin/env python3
"""Verify every configured harness through its dedicated router path."""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote


def _request(method, url, api_key, payload=None, timeout=120):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = json.dumps(payload).encode() if payload is not None else None
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data, headers=headers, method=method),
            timeout=timeout,
        ) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"{method} {url} -> HTTP {exc.code}: {detail}") from exc


def smoke(base_url, api_key=""):
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
            _request(
                "POST",
                f"{url}/chat/completions",
                api_key,
                {
                    "model": "local",
                    "messages": [{"role": "user", "content": "Reply OK"}],
                    "max_tokens": 1,
                    "temperature": 0,
                },
            )
            checked += 1
            print(f"[OK] {label}")
    print(f"Harness smoke complete: {checked} connection(s).")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:9000")
    parser.add_argument("--api-key", default=os.environ.get("A0_LMM_ROUTER_API_KEY", ""))
    args = parser.parse_args()
    smoke(args.base_url, args.api_key)


if __name__ == "__main__":
    main()

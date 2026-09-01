#!/usr/bin/env python3
"""Run sanitized live checks against pinned harness connections."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote


_VERSION_PROBES = {
    "hermes": {
        "command": "hermes",
        "installed_pattern": r"Hermes Agent v(\d+\.\d+\.\d+)",
        "stable_url": "https://api.github.com/repos/NousResearch/hermes-agent/releases/latest",
        "stable_field": "name",
        "stable_pattern": r"Hermes Agent v(\d+\.\d+\.\d+)",
        "source_url": "https://github.com/NousResearch/hermes-agent/releases/latest",
    },
    "agent_zero": {
        "command": "a0",
        "installed_pattern": r"(?m)^\s*(\d+\.\d+(?:\.\d+)?)\s*$",
        "stable_url": "https://api.github.com/repos/agent0ai/agent-zero/releases/latest",
        "stable_field": "tag_name",
        "stable_pattern": r"v?(\d+\.\d+(?:\.\d+)?)",
        "source_url": "https://github.com/agent0ai/agent-zero/releases/latest",
    },
    "pi": {
        "command": "pi",
        "installed_pattern": r"(?m)^\s*(\d+\.\d+\.\d+)\s*$",
        "stable_url": "https://registry.npmjs.org/@earendil-works%2fpi-coding-agent/latest",
        "stable_field": "version",
        "stable_pattern": r"(\d+\.\d+\.\d+)",
        "source_url": "https://www.npmjs.com/package/@earendil-works/pi-coding-agent",
    },
    "claude_code": {
        "command": "claude",
        "installed_pattern": r"(?m)^\s*(\d+\.\d+\.\d+)\s+\(Claude Code\)",
        "stable_url": "https://registry.npmjs.org/@anthropic-ai%2fclaude-code/latest",
        "stable_field": "version",
        "stable_pattern": r"(\d+\.\d+\.\d+)",
        "source_url": "https://www.npmjs.com/package/@anthropic-ai/claude-code",
    },
}
_HERMES_CANARY_PROMPT = "Reply with exactly: IMPERIUM_CANARY_OK"
_HERMES_CANARY_RESPONSE = "IMPERIUM_CANARY_OK"
_HERMES_CANARY_TOOLSET = "bot_room"  # Valid Hermes text-only toolset; "none" is rejected.


def _unknown(reason_code: str) -> dict:
    return {"status": "unknown", "evidence": "unverified", "reason_code": reason_code}


def _probe_installed(kind: str, *, which=shutil.which, run=subprocess.run) -> dict:
    profile = _VERSION_PROBES.get(kind)
    if profile is None:
        return _unknown("unsupported_harness")
    command = which(profile["command"])
    if not command:
        return _unknown("executable_not_found")
    try:
        result = run(
            [command, "--version"],
            shell=False,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return _unknown("probe_timeout")
    except OSError:
        return _unknown("probe_failed")
    if result.returncode != 0:
        return _unknown("probe_failed")
    output = (result.stdout or "")[-8192:] + (result.stderr or "")[-8192:]
    match = re.search(profile["installed_pattern"], output)
    if not match:
        return _unknown("version_unrecognized")
    return {"status": "pass", "evidence": "observed", "version": match.group(1)}


def _probe_stable(kind: str, *, opener=urllib.request.urlopen) -> dict:
    profile = _VERSION_PROBES.get(kind)
    if profile is None:
        return _unknown("unsupported_harness")
    request = urllib.request.Request(
        profile["stable_url"],
        headers={"Accept": "application/json", "User-Agent": "Imperium-Harness-Audit"},
    )
    try:
        with opener(request, timeout=10) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return _unknown("stable_lookup_failed")
    value = str(payload.get(profile["stable_field"]) or "") if isinstance(payload, dict) else ""
    match = re.search(profile["stable_pattern"], value)
    if not match:
        return _unknown("stable_version_unrecognized")
    return {
        "status": "pass",
        "evidence": "documented",
        "version": match.group(1),
        "source_url": profile["source_url"],
    }


def _stable_with_alignment(installed: dict, stable: dict) -> dict:
    result = dict(stable)
    result["alignment"] = "unknown"
    if installed.get("status") != "pass" or stable.get("status") != "pass":
        return result
    installed_version = str(installed.get("version") or "")
    stable_version = str(stable.get("version") or "")
    try:
        installed_parts = tuple(int(part) for part in installed_version.split("."))
        stable_parts = tuple(int(part) for part in stable_version.split("."))
    except ValueError:
        return result
    width = max(len(installed_parts), len(stable_parts))
    installed_parts += (0,) * (width - len(installed_parts))
    stable_parts += (0,) * (width - len(stable_parts))
    result["installed_version"] = installed_version
    result["alignment"] = (
        "current"
        if installed_parts == stable_parts
        else "behind"
        if installed_parts < stable_parts
        else "ahead"
    )
    return result


def _canary_environment(home: Path, api_key: str) -> dict[str, str]:
    secret_markers = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    env = {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in secret_markers)
    }
    env["HERMES_HOME"] = str(home)
    env["ROUTER_API_KEY"] = api_key or "local"
    return env


def _routing_request_count(
    router_base_url: str,
    api_key: str,
    harness_id: str,
    timeout: int,
) -> int | None:
    try:
        payload = _request(
            "GET",
            f"{router_base_url.rstrip('/')}/routing/analytics?limit=1",
            api_key,
            timeout=min(timeout, 10),
        )
    except RuntimeError:
        return None
    by_app = payload.get("by_app") if isinstance(payload, dict) else None
    value = by_app.get(harness_id, 0) if isinstance(by_app, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _run_hermes_canary(
    setup_content: str,
    connection_url: str,
    router_base_url: str,
    api_key: str,
    timeout: int,
    *,
    which=shutil.which,
    run=subprocess.run,
    request_count=_routing_request_count,
) -> dict:
    command = which("hermes")
    if not command:
        return _unknown("executable_not_found")
    if not setup_content.strip():
        return _unknown("setup_manifest_missing")
    canary_setup, replacements = re.subn(
        r"(?m)^(\s*base_url:\s*).+$",
        lambda match: f"{match.group(1)}{connection_url}",
        setup_content,
    )
    if replacements < 1:
        return _unknown("setup_base_url_missing")
    before_count = request_count(router_base_url, api_key, "hermes", timeout)
    try:
        with tempfile.TemporaryDirectory(prefix="imperium-hermes-canary-") as directory:
            root = Path(directory)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            (home / "config.yaml").write_text(canary_setup, encoding="utf-8")
            result = run(
                [
                    command,
                    "-z",
                    _HERMES_CANARY_PROMPT,
                    "-m",
                    "local",
                    "--provider",
                    "imperium",
                    "-t",
                    _HERMES_CANARY_TOOLSET,
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--in",
                    str(workspace),
                ],
                shell=False,
                cwd=workspace,
                env=_canary_environment(home, api_key),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                stdin=subprocess.DEVNULL,
            )
    except subprocess.TimeoutExpired:
        return _unknown("client_canary_timeout")
    except OSError:
        return _unknown("client_canary_failed")
    if result.returncode != 0:
        return _unknown("client_canary_failed")
    if _HERMES_CANARY_RESPONSE not in (result.stdout or ""):
        return _unknown("unexpected_response")
    after_count = request_count(router_base_url, api_key, "hermes", timeout)
    if before_count is None or after_count is None or after_count <= before_count:
        return {
            "status": "unknown",
            "evidence": "unverified",
            "reason_code": "routing_unverified",
            "client_status": "pass",
        }
    return {"status": "pass", "evidence": "tested", "routing": "verified"}


def _evidence_status(row: dict) -> str:
    required = ("endpoint", "installed", "client_canary")
    return "pass" if all((row.get(key) or {}).get("status") == "pass" for key in required) else "unknown"


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
    collect_evidence: bool = False,
    client_canary_ids: set[str] | None = None,
    require_complete_evidence: bool = False,
):
    base_url = base_url.rstrip("/")
    report = {
        "schema_version": 2,
        "kind": "harness_smoke",
        "ok": False,
        "endpoint_ok": False,
        "required_harnesses": sorted(harness_ids or []),
        "connections": [],
        "evidence": {},
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
            kind = str(harness.get("kind") or harness_id)
            evidence = {
                "endpoint": _unknown("endpoint_smoke_not_run"),
                "installed": _unknown("version_probe_not_requested"),
                "stable": _unknown("stable_lookup_not_requested"),
                "client_canary": _unknown("client_canary_not_requested"),
            }
            report["evidence"][harness_id] = evidence
            endpoint_connections = []
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
                endpoint_connections.append(name)
                print(f"[OK] {label}")
            evidence["endpoint"] = {
                "status": "pass",
                "evidence": "tested",
                "connections": endpoint_connections,
            }
            if collect_evidence:
                evidence["installed"] = _probe_installed(kind)
                evidence["stable"] = _stable_with_alignment(
                    evidence["installed"], _probe_stable(kind)
                )
            if client_canary_ids and harness_id in client_canary_ids:
                if kind == "hermes":
                    setup = harness.get("setup") or {}
                    evidence["client_canary"] = _run_hermes_canary(
                        str(setup.get("content") or ""),
                        _connection_url(base_url, harness_id, "default"),
                        base_url,
                        api_key,
                        timeout,
                    )
                else:
                    evidence["client_canary"] = _unknown("client_canary_not_implemented")
            evidence["overall"] = _evidence_status(evidence)
        if not report["connections"]:
            raise RuntimeError("no_harness_connections_matched")
        report["endpoint_ok"] = True
        incomplete = sorted(
            harness_id
            for harness_id, evidence in report["evidence"].items()
            if evidence.get("overall") != "pass"
        )
        if incomplete:
            report["incomplete_harnesses"] = incomplete
        if require_complete_evidence and incomplete:
            raise RuntimeError("required_harness_evidence_incomplete")
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
    parser.add_argument("--versions", action="store_true", help="record installed and current stable client versions")
    parser.add_argument(
        "--client-canary",
        action="append",
        default=[],
        choices=["hermes"],
        help="run an isolated real-client canary (repeatable)",
    )
    parser.add_argument("--json-output", help="write a sanitized machine-readable result")
    parser.add_argument("--harness", action="append", default=[], help="required harness id (repeatable)")
    parser.add_argument("--timeout", type=int, default=180, help="per-request timeout seconds")
    parser.add_argument("--max-tokens", type=int, default=256, help="completion budget")
    args = parser.parse_args()
    if args.rc and (args.no_stream or not args.harness):
        parser.error("--rc requires at least one --harness and does not allow --no-stream")
    if set(args.client_canary) - set(args.harness):
        parser.error("--client-canary must also be selected with --harness")
    try:
        canaries = set(args.harness) if args.rc else set(args.client_canary)
        smoke(
            args.base_url,
            args.api_key,
            check_stream=not args.no_stream,
            check_tools=args.tools or args.rc,
            harness_ids=set(args.harness) or None,
            timeout=args.timeout,
            max_tokens=args.max_tokens,
            json_output=args.json_output,
            collect_evidence=args.versions or args.rc or bool(canaries),
            client_canary_ids=canaries,
            require_complete_evidence=args.rc,
        )
    except RuntimeError as exc:
        print(f"[FAIL] harness smoke: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

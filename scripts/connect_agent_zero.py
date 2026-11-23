#!/usr/bin/env python3
"""Connect a running Agent Zero container to Imperium in one command.

Reads the Agent Zero connections Imperium already publishes (``GET /harnesses``),
writes an "Imperium (local)" model preset into Agent Zero's bind-mounted
``usr`` directory, and selects it once Imperium reports the pinned model ready.
Re-running is safe: the preset is replaced, nothing else is touched.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import yaml

PRESET_NAME = "Imperium (local)"
KEY_NAME = "API_KEY_OTHER"  # where Agent Zero stores the "Other OpenAI compatible" key
DEFAULT_CTX = 32768  # conservative when the router cannot tell us the slot's context


def _get(router: str, path: str) -> dict:
    req = urllib.request.Request(router.rstrip("/") + path)
    key = os.environ.get("A0_LMM_ROUTER_API_KEY", "").strip()
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def _docker(*args: str) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout


def find_container(container: str | None = None) -> str:
    """The (single) running Agent Zero container, unless one was named."""
    if container:
        return container
    rows = [r.split("\t") for r in _docker("ps", "--format", "{{.Names}}\t{{.Image}}").splitlines()]
    names = [name for name, image in rows if "agent-zero" in image]
    if len(names) != 1:
        sys.exit(f"found {len(names)} Agent Zero containers {names}; pass --container or --usr")
    return names[0]


def find_usr(container: str) -> Path:
    """Host path bind-mounted at /a0/usr of the Agent Zero container."""
    mounts = _docker("inspect", "-f", "{{range .Mounts}}{{.Destination}}|{{.Source}}\n{{end}}", container)
    for line in mounts.splitlines():
        dest, _, src = line.partition("|")
        if dest == "/a0/usr":
            return Path(src)
    sys.exit(f"{container} has no bind mount at /a0/usr; pass --usr with the host path")


def slot_contexts(backends: dict) -> dict[str, int]:
    """Context window of every local slot, keyed by slot id and by model_id.

    A harness may pin either. A slot-id pin (``slot_chat``) follows a model swap,
    so its context has to come from the slot, not from the old model's name.
    """
    out: dict[str, int] = {}
    for backend in backends.get("backends", []):
        for slot in backend.get("slots", []):
            ctx = slot.get("effective_context_size") or slot.get("context_size")
            for key in (slot.get("id"), slot.get("model_id")):
                if key and ctx:
                    out[key] = int(ctx)
    return out


def build_preset(harness: dict, ctx_by_model: dict[str, int]) -> dict:
    """One Agent Zero preset whose chat/utility models are Imperium's pinned connections."""
    conns = {c["name"]: c for c in harness["connections"]}
    preset: dict = {"name": PRESET_NAME}
    for slot in ("chat", "utility"):
        c = conns[slot]
        preset[slot] = {
            "provider": "other",  # Agent Zero's "Other OpenAI compatible"
            "name": c["client_model"],
            "api_base": c["base_url"],
            "ctx_length": ctx_by_model.get(c["model"], DEFAULT_CTX),
        }
    preset["chat"]["vision"] = bool(conns["chat"]["capabilities"].get("vision"))
    return preset


def upsert_preset(presets: list, preset: dict) -> list:
    return [p for p in presets if p.get("name") != preset["name"]] + [preset]


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def ensure_api_key(env_path: Path, value: str = "local") -> bool:
    """Set the shared 'other' key only if empty; never overwrite a real one."""
    text = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    lines = text.splitlines()
    for line in lines:
        k, _, v = line.partition("=")
        if k == KEY_NAME and v.strip() not in ("", "None"):
            return False
    lines = [ln for ln in lines if ln.partition("=")[0] != KEY_NAME]
    _write(env_path, "\n".join(lines + [f"{KEY_NAME}={value}"]) + "\n")
    return True


def run(harness: dict, ctx_by_model: dict[str, int], usr: Path, *, select: bool = True, dry_run: bool = False) -> dict:
    plugin = usr / "plugins" / "_model_config"
    presets_path = plugin / "presets.yaml"
    if not presets_path.is_file():
        sys.exit(f"{presets_path} not found - open Agent Zero once so it creates its presets, then retry")

    ready = all(c["readiness"]["model_ready"] for c in harness["connections"])
    result = {
        "preset": build_preset(harness, ctx_by_model),
        "ready": ready,
        "selected": select and ready,
        "key_written": False,
    }
    if dry_run:
        return result

    backup = presets_path.with_name(presets_path.name + ".pre-imperium.bak")
    if not backup.exists():
        shutil.copy2(presets_path, backup)
    presets = yaml.safe_load(presets_path.read_text(encoding="utf-8")) or []
    _write(presets_path, yaml.safe_dump(upsert_preset(presets, result["preset"]), sort_keys=False, allow_unicode=True))

    if not harness.get("authentication_required"):
        result["key_written"] = ensure_api_key(usr / ".env")
    if result["selected"]:
        cfg = plugin / "config.json"
        data = json.loads(cfg.read_text(encoding="utf-8")) if cfg.is_file() else {}
        data["model_preset"] = PRESET_NAME
        _write(cfg, json.dumps(data, indent=2) + "\n")
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--router", default="http://127.0.0.1:9000", help="Imperium base URL")
    ap.add_argument("--container", help="Agent Zero container name (auto-detected if omitted)")
    ap.add_argument("--usr", type=Path, help="host path of Agent Zero's /a0/usr (skips docker detection)")
    ap.add_argument("--no-select", action="store_true", help="add the preset but leave the active one alone")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    ap.add_argument("--restart", action="store_true", help="restart Agent Zero's UI process so it reloads .env")
    a = ap.parse_args(argv)

    harnesses = _get(a.router, "/harnesses")["harnesses"]
    harness = next((h for h in harnesses if h["harness_id"] == "agent_zero"), None)
    if harness is None:
        sys.exit("Imperium has no agent_zero harness (check conf/harnesses.yaml)")
    container = a.container or (None if a.usr else find_container())
    usr = a.usr or find_usr(container)
    r = run(harness, slot_contexts(_get(a.router, "/backends")), usr, select=not a.no_select, dry_run=a.dry_run)

    chat = r["preset"]["chat"]
    print(f"{'would write' if a.dry_run else 'wrote'} preset '{PRESET_NAME}' -> {usr}")
    print(f"  chat/utility via {chat['api_base'].rsplit('/chat/', 1)[0]}/..., context {chat['ctx_length']}")
    if r["key_written"]:
        print(f"  set {KEY_NAME}=local (router auth is off)")
    elif harness.get("authentication_required"):
        print(f"  router auth is ON: set the Imperium key under A0 Settings > External Services > Other OpenAI-compatible")
    if not r["ready"]:
        print("  NOT selected: Imperium says the pinned model is not ready (restart the router after fixing conf/harnesses.yaml)")
    elif r["selected"] and not a.dry_run:
        print("  selected as the active preset - open a new chat in Agent Zero")
    if not a.dry_run:
        # Agent Zero reads usr/.env into its environment only at startup, so a key written
        # above is invisible to an already-running UI ("Missing credentials" on first use).
        restart = ["exec", container, "supervisorctl", "restart", "run_ui"] if container else None
        if a.restart and restart:
            _docker(*restart)
            print("  restarted Agent Zero's UI so it reloads .env - refresh the page")
        else:
            cmd = f"docker {' '.join(restart)}" if restart else "restart Agent Zero's UI process"
            print(f"  if Agent Zero was already running, reload its .env: {cmd}  (or pass --restart), then refresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())

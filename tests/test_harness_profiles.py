"""Harness profile loading and persistence."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from local_model_router.harnesses.profiles import (
    HarnessConfigError,
    HarnessProfiles,
)
from local_model_router.harnesses.setup import setup_manifest


def _write(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def test_loads_single_and_agent_zero_connections(tmp_path):
    profiles = HarnessProfiles.load(_write(tmp_path / "harnesses.yaml", """
        harnesses:
          hermes:
            display_name: Hermes
            kind: hermes
            protocol: openai
            location: host
            connections:
              default: {model: dmr/ornith}
          agent_zero:
            display_name: Agent Zero
            kind: agent_zero
            protocol: openai
            location: docker
            connections:
              chat: {model: dmr/ornith}
              utility: {model: utility_cpu}
    """))

    assert profiles.resolve("hermes").model == "dmr/ornith"
    assert profiles.resolve("agent_zero", "utility").model == "utility_cpu"
    assert profiles.get("agent_zero").location == "docker"


@pytest.mark.parametrize("bad_id", ["Hermes", "bad id", "../escape", ""])
def test_rejects_invalid_harness_ids(tmp_path, bad_id):
    path = _write(tmp_path / "harnesses.yaml", f"""
        harnesses:
          "{bad_id}":
            connections:
              default: {{model: local}}
    """)
    with pytest.raises(HarnessConfigError, match="harness id"):
        HarnessProfiles.load(path)


def test_rejects_empty_model_and_invalid_connection_name(tmp_path):
    empty = _write(tmp_path / "empty.yaml", """
        harnesses:
          hermes:
            connections:
              default: {model: ""}
    """)
    with pytest.raises(HarnessConfigError, match="model"):
        HarnessProfiles.load(empty)

    invalid = _write(tmp_path / "invalid.yaml", """
        harnesses:
          hermes:
            connections:
              "Chat Role": {model: local}
    """)
    with pytest.raises(HarnessConfigError, match="connection"):
        HarnessProfiles.load(invalid)


def test_multi_connection_harness_requires_explicit_connection(tmp_path):
    profiles = HarnessProfiles.load(_write(tmp_path / "harnesses.yaml", """
        harnesses:
          agent_zero:
            connections:
              chat: {model: chat-model}
              utility: {model: utility-model}
    """))
    with pytest.raises(KeyError):
        profiles.resolve("agent_zero")


def test_falls_back_to_legacy_apps_when_canonical_file_is_missing(tmp_path):
    legacy = _write(tmp_path / "apps.yaml", """
        apps:
          hermes:
            display_name: Hermes
            default_model: chat
            roles:
              chat: dmr/ornith
          agent_zero:
            display_name: Agent Zero
            roles:
              chat: dmr/ornith
              utility: utility_cpu
    """)
    profiles = HarnessProfiles.load(tmp_path / "missing.yaml", legacy_path=legacy)

    assert profiles.resolve("hermes").model == "dmr/ornith"
    assert profiles.resolve("agent_zero", "chat").model == "dmr/ornith"
    assert profiles.source == "legacy_apps"


def test_atomic_upsert_creates_backup(tmp_path):
    path = _write(tmp_path / "harnesses.yaml", """
        harnesses:
          hermes:
            connections:
              default: {model: old-model}
    """)
    profiles = HarnessProfiles.load(path)
    updated, backup = profiles.upsert({
        "harness_id": "pi",
        "display_name": "Pi",
        "kind": "pi",
        "protocol": "openai",
        "location": "host",
        "connections": {"default": {"model": "utility_cpu"}},
    })

    assert backup is not None and backup.exists()
    assert HarnessProfiles.load(path).resolve("pi").model == "utility_cpu"
    assert updated.resolve("hermes").model == "old-model"
    assert not list(tmp_path.glob("*.tmp"))


def test_committed_profiles_cover_current_harnesses_and_claude_adapter():
    path = Path(__file__).resolve().parents[1] / "conf" / "harnesses.yaml"
    profiles = HarnessProfiles.load(path)
    assert {item.harness_id for item in profiles.list_profiles()} == {
        "agent_zero", "claude_code_local", "hermes", "pi",
    }
    claude = profiles.get("claude_code_local")
    manifest = setup_manifest(claude, auth_required=False)
    assert "LiteLLM" in manifest["setup"]["target"]
    setup = manifest["setup"]["content"]
    assert "litellm --config" in setup
    assert "ANTHROPIC_BASE_URL" in setup

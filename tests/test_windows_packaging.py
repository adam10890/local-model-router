from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_launchers_persist_settings_and_identify_the_router():
    start = (ROOT / "START.bat").read_text(encoding="utf-8")
    stop = (ROOT / "STOP.bat").read_text(encoding="utf-8")

    assert '%IMPERIUM_HOME%\\.env' in start
    assert '%IMPERIUM_HOME%\\.env' in stop
    assert "$health.service -eq 'lmm-router-observer'" in start
    assert "exit /b %errorlevel%" not in start
    assert "if errorlevel 1 exit /b 1" in start
    assert "netstat" not in stop.lower()


def test_bundle_requires_license_agents_and_versioned_hash():
    build = (ROOT / "scripts" / "build_windows_bundle.ps1").read_text(encoding="utf-8")
    archiver = (ROOT / "scripts" / "zip_tree.py").read_text(encoding="utf-8")

    assert (ROOT / "LICENSE").is_file()
    assert (ROOT / "THIRD_PARTY_NOTICES.md").is_file()
    assert (ROOT / "licenses" / "Apache-2.0.txt").is_file()
    assert "OpenAIChatModel" in build and "OpenAIProvider" in build
    assert '"$Zip.sha256"' in build
    assert 'Requested bundle version' in build
    assert "--no-build-isolation" in build
    assert "DependencySeed" in build
    assert '$_.Name -notlike "local_model_router*" -and $_.Name -ne "bin"' in build
    assert 'Join-Path $TargetPackages "bin"' in build
    assert "--no-compile" in build
    assert 'Filter "__pycache__"' in build
    assert 'Filter "direct_url.json"' in build
    assert "Rollback-Imperium.ps1" in build
    assert 'Join-Path $PSScriptRoot "zip_tree.py"' in build
    assert "ZIP_STORED" in archiver and "allowZip64=True" in archiver


def test_cleanroom_verifies_checksum_and_live_installed_surfaces():
    cleanroom = (ROOT / "scripts" / "test_windows_cleanroom.ps1").read_text(encoding="utf-8")

    assert "Test-StrictChildPath" in cleanroom
    assert "$Root == $BuildRoot" not in cleanroom
    assert "Get-FileHash -LiteralPath $Bundle" in cleanroom
    assert 'IMPERIUM_OFFLINE = "1"' in cleanroom
    assert 'http://127.0.0.1:9100/agents' in cleanroom
    assert 'agents/code-review/runs' in cleanroom
    assert 'Health.service -ne "lmm-router-observer"' in cleanroom


def test_application_rollback_swaps_only_fixed_per_user_paths():
    rollback = (ROOT / "installer" / "windows" / "Rollback-Imperium.ps1").read_text(encoding="utf-8")

    assert "Test-StrictChildPath" in rollback
    assert 'Programs "Imperium"' in rollback
    assert 'Programs "Imperium.previous"' in rollback
    assert "Move-Item -LiteralPath $Previous -Destination $Target" in rollback
    assert 'WINDOWTITLE eq Imperium - Local Model Router' in rollback

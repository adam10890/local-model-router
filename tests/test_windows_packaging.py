from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_launchers_persist_settings_and_identify_the_router():
    start = (ROOT / "START.bat").read_text(encoding="utf-8")
    stop = (ROOT / "STOP.bat").read_text(encoding="utf-8")

    assert '%IMPERIUM_HOME%\\.env' in start
    assert '%IMPERIUM_HOME%\\.env' in stop
    assert "$health.service -eq 'lmm-router-observer'" in start
    assert "Get-NetTCPConnection -LocalPort %OBSERVER_PORT%" in stop
    assert "local_model_router\\s+serve" in stop
    assert "timeout /t" not in stop.lower()
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
    assert 'http://127.0.0.1:9100/health' in cleanroom
    assert 'http://127.0.0.1:9100/ui/status' in cleanroom
    assert 'http://127.0.0.1:9100/v1/models' in cleanroom
    assert "setup --repair" not in cleanroom  # lifecycle RC is operator checklist, not cleanroom
    assert "Uninstall-Imperium" not in cleanroom


def test_lifecycle_recovery_commands_are_documented_for_operators():
    evidence = (ROOT / "docs" / "1.0-beta-evidence.md").read_text(encoding="utf-8")
    cli = (ROOT / "local_model_router" / "cli.py").read_text(encoding="utf-8")

    for needle in (
        "setup --repair",
        "imperium doctor",
        "imperium update",
        "imperium rollback",
        "Uninstall-Imperium",
        "Rollback-Imperium",
        "test_windows_cleanroom.ps1",
    ):
        assert needle in evidence
    assert 'setup.add_argument("--repair"' in cli
    assert 'sub.add_parser("rollback"' in cli
    assert 'sub.add_parser("update"' in cli
    assert 'sub.add_parser("doctor"' in cli


def test_application_rollback_swaps_only_fixed_per_user_paths():
    rollback = (ROOT / "installer" / "windows" / "Rollback-Imperium.ps1").read_text(encoding="utf-8")

    assert "Test-StrictChildPath" in rollback
    assert 'Programs "Imperium"' in rollback
    assert 'Programs "Imperium.previous"' in rollback
    assert "Move-Item -LiteralPath $Previous -Destination $Target" in rollback
    assert 'Join-Path $Target "STOP.bat"' in rollback


def test_application_uninstall_is_path_scoped_and_preserves_settings():
    uninstall = (ROOT / "installer" / "windows" / "Uninstall-Imperium.ps1").read_text(encoding="utf-8")
    launcher = (ROOT / "installer" / "windows" / "Uninstall-Imperium.bat").read_text(encoding="utf-8")
    build = (ROOT / "scripts" / "build_windows_bundle.ps1").read_text(encoding="utf-8")

    assert "Test-StrictChildPath" in uninstall
    assert 'Programs "Imperium"' in uninstall
    assert 'Programs "Imperium.previous"' in uninstall
    assert 'Join-Path $Target "STOP.bat"' in uninstall
    assert "setup --stop-runtime" in uninstall
    assert "Models and settings remain under %LOCALAPPDATA%\\Imperium." in uninstall
    assert "Uninstall-Imperium.ps1" in launcher
    assert "Uninstall-Imperium.bat" in build

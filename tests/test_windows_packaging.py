from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_launchers_persist_settings_and_identify_the_router():
    start = (ROOT / "START.bat").read_text(encoding="utf-8")
    stop = (ROOT / "STOP.bat").read_text(encoding="utf-8")

    assert '%IMPERIUM_HOME%\\.env' in start
    assert '%IMPERIUM_HOME%\\.env' in stop
    assert "$health.service -eq 'lmm-router-observer'" in start
    assert '"%PYTHON%" -m local_model_router shutdown' in stop
    assert "Get-NetTCPConnection" not in stop
    assert "router_ownership_unverified" in (ROOT / "local_model_router" / "cli.py").read_text(
        encoding="utf-8"
    )
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
    assert "Get-FileHash -LiteralPath $Archive" in cleanroom
    assert 'IMPERIUM_OFFLINE = "1"' in cleanroom
    assert 'http://127.0.0.1:9100/agents' in cleanroom
    assert 'agents/code-review/runs' in cleanroom
    assert 'Health.service -ne "lmm-router-observer"' in cleanroom
    assert 'http://127.0.0.1:9100/health' in cleanroom
    assert 'http://127.0.0.1:9100/ui/status' in cleanroom
    assert 'http://127.0.0.1:9100/v1/models' in cleanroom
    assert "setup --repair --yes" in cleanroom
    assert "update --check" in cleanroom
    assert 'Join-Path $Target "Rollback-Imperium.ps1"' in cleanroom
    assert 'Join-Path $Target "Uninstall-Imperium.ps1"' in cleanroom
    assert "-NoShortcut" in cleanroom
    assert cleanroom.rindex("finally {") < cleanroom.rindex("setup --stop-runtime")
    assert cleanroom.count("finally {") >= 2
    assert "cleanroom-preserve.txt" in cleanroom
    assert "bundle_name" in cleanroom
    assert "runtime_transition" in cleanroom
    assert "rollback = $RolledBack.runtime.tag" in cleanroom
    assert "private_python = $Python" not in cleanroom
    assert "installed = $Target" not in cleanroom


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
    assert "param([switch]$NoShortcut)" in uninstall
    assert "if (-not $NoShortcut)" in uninstall
    assert "Models and settings remain under %LOCALAPPDATA%\\Imperium." in uninstall
    assert "Uninstall-Imperium.ps1" in launcher
    assert "Uninstall-Imperium.bat" in build


def test_windows_validation_separates_pr_nightly_and_nvidia_evidence():
    workflow = (ROOT / ".github" / "workflows" / "windows-validation.yml").read_text(
        encoding="utf-8"
    )
    release_workflow = (ROOT / ".github" / "workflows" / "windows-bundle.yml").read_text(
        encoding="utf-8"
    )

    assert "pull_request:" in workflow and "schedule:" in workflow
    assert 'python-version: "3.12"' in workflow
    assert "coverage report --fail-under=75" in workflow
    assert "test_windows_cleanroom.ps1" in workflow
    assert "-FullOfflineSetup -Backend cpu -Lifecycle" in workflow
    assert "self-hosted, windows, x64, nvidia" in workflow
    assert "cleanroom-result.json" in workflow and "provider-smoke.json" in workflow
    assert "-Lifecycle -HarnessCanary" in workflow
    assert "@earendil-works/pi-coding-agent@" in workflow
    assert "harness-smoke.json" in workflow
    assert "push:" not in release_workflow
    assert "gh release create" not in release_workflow


def test_cleanroom_harness_canary_pins_only_pi_and_runs_rc_smoke():
    cleanroom = (ROOT / "scripts" / "test_windows_cleanroom.ps1").read_text(encoding="utf-8")

    assert "[switch]$HarnessCanary" in cleanroom
    assert "-HarnessCanary needs -FullOfflineSetup" in cleanroom
    assert "$env:A0_LMM_ROUTER_HARNESSES_CONFIG = $HarnessConfig" in cleanroom
    assert "model: qwen3-1.7b-q8" in cleanroom
    assert "--rc --harness pi" in cleanroom
    # The client is resolved before the clean-room PATH narrowing and restored after the call.
    assert cleanroom.index("$CanaryClientPath = ") < cleanroom.index('$env:PATH = "$env:SystemRoot')
    assert "$env:PATH = $CleanPath" in cleanroom
    # The config must be in place before the router reads it at startup.
    assert cleanroom.index("A0_LMM_ROUTER_HARNESSES_CONFIG") < cleanroom.index('"local_model_router", "serve"')


def test_provider_smokes_are_machine_readable_and_rc_strict():
    powershell = (ROOT / "scripts" / "smoke_provider.ps1").read_text(encoding="utf-8")
    shell = (ROOT / "scripts" / "smoke_provider.sh").read_text(encoding="utf-8")

    for script in (powershell, shell):
        assert "provider_smoke" in script
        assert "health" in script and "models" in script and "fleet" in script
        assert "route" in script and "chat" in script and "stream" in script
        assert "tools" in script and "imperium_ping" in script
        assert "enable_thinking" in script
        assert "json" in script.lower()
        assert "require" in script.lower() and "live" in script.lower()
    assert "Body: $content" not in powershell
    assert 'cat "$tmp"' not in shell


def _write_disposable_bundle(bundle_dir: Path, version: str) -> None:
    import json
    import os
    import shutil
    import stat
    import sys

    bundle_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "Install-Imperium.ps1",
        "Install-Imperium.bat",
        "Rollback-Imperium.ps1",
        "Rollback-Imperium.bat",
        "Uninstall-Imperium.ps1",
        "Uninstall-Imperium.bat",
    ):
        shutil.copy2(ROOT / "installer" / "windows" / name, bundle_dir / name)
    (bundle_dir / "STOP.bat").write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
    (bundle_dir / "START.bat").write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
    (bundle_dir / "bundle-manifest.json").write_text(
        json.dumps({"version": version}), encoding="utf-8"
    )
    py_dir = bundle_dir / "runtime" / "python"
    py_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        shutil.copy2(Path(sys.executable), py_dir / "python.exe")
        cfg = f"home = {Path(sys.base_prefix)}\ninclude-system-site-packages = false\n"
        (bundle_dir / "runtime" / "pyvenv.cfg").write_text(cfg, encoding="utf-8")
        pkg_dir = bundle_dir / "runtime" / "Lib" / "site-packages" / "local_model_router"
    else:
        shim = py_dir / "python.exe"
        shim.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'site-packages'))\n"
            "import runpy\n"
            "if sys.argv[1:3] == ['-m', 'local_model_router']:\n"
            "    sys.argv = ['local_model_router'] + sys.argv[3:]\n"
            "    runpy.run_module('local_model_router', run_name='__main__')\n"
            "elif sys.argv[1:2] == ['-c']:\n"
            "    exec(sys.argv[2])\n",
            encoding="utf-8",
        )
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        pkg_dir = py_dir / "site-packages" / "local_model_router"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (pkg_dir / "__main__.py").write_text(
        'import sys\nif "--help" in sys.argv:\n    print("usage: imperium")\n    raise SystemExit(0)\nraise SystemExit(0)\n',
        encoding="utf-8",
    )


def test_shipped_installer_recovers_failed_promotion_and_preserves_backup_and_raw_path(tmp_path):
    import os
    import shutil
    import subprocess
    import sys

    shell = (
        shutil.which("powershell.exe") or shutil.which("pwsh")
        if os.name == "nt"
        else shutil.which("pwsh")
    )
    assert shell, "PowerShell runtime is required to exercise Windows installer scripts"

    v1 = tmp_path / "bundle-v1"
    v2 = tmp_path / "bundle-v2"
    _write_disposable_bundle(v1, "0.0.1")
    _write_disposable_bundle(v2, "0.0.2")
    local_app_data = tmp_path / "LocalAppData"
    local_app_data.mkdir()

    raw_reg_before = None
    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            try:
                raw_reg_before = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                raw_reg_before = None

    unexpanded_entry = r"%IMPERIUM_UNEXPANDED_SENTINEL%\tools\bin"
    base_env = {
        **os.environ,
        "LOCALAPPDATA": str(local_app_data),
        "PATH": unexpanded_entry + os.pathsep + os.environ.get("PATH", ""),
    }
    base_env.pop("PYTHONPATH", None)
    base_env.pop("PYTHONHOME", None)
    base_env.pop("IMPERIUM_TEST_FAIL_PROMOTE", None)
    base_env.pop("IMPERIUM_TEST_FAIL_ROLLBACK", None)
    base_env.pop("IMPERIUM_TEST_FAIL_ROLLBACK_RESTORE", None)

    def run_ps(script: Path, extra_args: list[str], extra_env: dict[str, str] | None = None):
        env = {**base_env, **(extra_env or {})}
        return subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *extra_args],
            cwd=str(tmp_path),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )

    def installed_version(root_dir: Path) -> str:
        init_candidates = list(root_dir.rglob("local_model_router/__init__.py"))
        assert len(init_candidates) == 1
        text = init_candidates[0].read_text(encoding="utf-8")
        return text.split('"')[1]

    target = local_app_data / "Programs" / "Imperium"
    previous = local_app_data / "Programs" / "Imperium.previous"

    # 1. Initial install via Install-Imperium.bat (on Windows) or .ps1.
    if os.name == "nt":
        res1 = subprocess.run(
            ["cmd.exe", "/c", str(v1 / "Install-Imperium.bat"), "-NoShortcut", "-NoLaunch"],
            cwd=str(tmp_path),
            env=base_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        res1 = run_ps(v1 / "Install-Imperium.ps1", ["-NoShortcut", "-NoLaunch"])
    assert res1.returncode == 0, res1.stderr or res1.stdout
    assert installed_version(target) == "0.0.1"

    # 2. Failed promotion restores v1 at Imperium and cleans up staging.
    fail_prom = run_ps(
        v2 / "Install-Imperium.ps1",
        ["-NoShortcut", "-NoLaunch"],
        {"IMPERIUM_TEST_FAIL_PROMOTE": "1"},
    )
    assert fail_prom.returncode != 0
    assert target.is_dir()
    assert installed_version(target) == "0.0.1"
    assert not list((local_app_data / "Programs").glob("Imperium.staging-*"))

    # 3. Failed promotion + failed rollback preserves backup at Imperium.previous and reports path.
    fail_both = run_ps(
        v2 / "Install-Imperium.ps1",
        ["-NoShortcut", "-NoLaunch"],
        {"IMPERIUM_TEST_FAIL_PROMOTE": "1", "IMPERIUM_TEST_FAIL_ROLLBACK": "1"},
    )
    assert fail_both.returncode != 0
    combined_err = (fail_both.stderr or "") + (fail_both.stdout or "")
    assert "Recoverable backup preserved at:" in combined_err
    assert str(previous) in combined_err
    assert not target.exists()
    assert previous.is_dir()
    assert installed_version(previous) == "0.0.1"

    # 4. Re-running while Target is absent must NOT clobber Imperium.previous if promotion fails again.
    retry_fail = run_ps(
        v2 / "Install-Imperium.ps1",
        ["-NoShortcut", "-NoLaunch"],
        {"IMPERIUM_TEST_FAIL_PROMOTE": "1", "IMPERIUM_TEST_FAIL_ROLLBACK": "1"},
    )
    assert retry_fail.returncode != 0
    assert previous.is_dir()
    assert installed_version(previous) == "0.0.1"

    # 5. Clean upgrade succeeds, keeping v1 at Imperium.previous and v2 at Imperium.
    ok_v2 = run_ps(v2 / "Install-Imperium.ps1", ["-NoShortcut", "-NoLaunch"])
    assert ok_v2.returncode == 0, ok_v2.stderr or ok_v2.stdout
    assert installed_version(target) == "0.0.2"
    assert installed_version(previous) == "0.0.1"

    # 6. Failed Rollback-Imperium.ps1 + failed restore preserves backup and reports surviving path.
    if os.name == "nt":
        fail_rb = run_ps(
            target / "Rollback-Imperium.ps1",
            [],
            {"IMPERIUM_TEST_FAIL_ROLLBACK": "1", "IMPERIUM_TEST_FAIL_ROLLBACK_RESTORE": "1"},
        )
        assert fail_rb.returncode != 0
        rb_err = (fail_rb.stderr or "") + (fail_rb.stdout or "")
        assert "Recoverable backup preserved at:" in rb_err
        assert previous.is_dir()

    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            try:
                raw_reg_after = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                raw_reg_after = None
        assert raw_reg_after == raw_reg_before

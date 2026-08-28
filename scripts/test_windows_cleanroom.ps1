param(
    [string]$Bundle = "",
    [string]$PreviousBundle = "",
    [string]$Root = "",
    [string]$ResultPath = "",
    [ValidateSet("cpu", "cuda12")][string]$Backend = "cpu",
    [switch]$Launch,
    [switch]$FullOfflineSetup,
    [switch]$Lifecycle,
    [switch]$KeepInstalled
)

$ErrorActionPreference = "Stop"
$Repo = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$BuildRoot = [IO.Path]::GetFullPath((Join-Path $Repo "build"))

function Test-StrictChildPath([string]$Parent, [string]$Child) {
    $Relative = [IO.Path]::GetRelativePath([IO.Path]::GetFullPath($Parent), [IO.Path]::GetFullPath($Child))
    return $Relative -ne "." -and -not [IO.Path]::IsPathRooted($Relative) -and
        $Relative -ne ".." -and -not $Relative.StartsWith("..$([IO.Path]::DirectorySeparatorChar)")
}

function Get-VerifiedBundleHash([string]$Archive) {
    if (-not (Test-Path -LiteralPath $Archive)) { throw "The requested Windows bundle is missing." }
    $Sidecar = "$Archive.sha256"
    if (-not (Test-Path -LiteralPath $Sidecar)) { throw "The bundle SHA-256 sidecar is missing." }
    $Expected = ((Get-Content -LiteralPath $Sidecar -Raw).Trim() -split "\s+")[0].ToLowerInvariant()
    $Actual = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Expected -ne $Actual) { throw "The bundle SHA-256 does not match its sidecar." }
    return $Actual
}

if (-not $Root) { $Root = Join-Path $BuildRoot "cleanroom" }
$Root = [IO.Path]::GetFullPath($Root)
if (-not (Test-StrictChildPath $BuildRoot $Root)) {
    throw "The clean-room root must stay inside the repository build directory."
}
if (-not $Bundle) {
    $Bundle = Get-ChildItem -LiteralPath (Join-Path $BuildRoot "windows") -Filter "Imperium-*-windows-x64.zip" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $Bundle) { throw "Build the Windows bundle before running the clean-room test." }
$Bundle = [IO.Path]::GetFullPath($Bundle)
$CandidateHash = Get-VerifiedBundleHash $Bundle
if ($PreviousBundle) {
    $PreviousBundle = [IO.Path]::GetFullPath($PreviousBundle)
    Get-VerifiedBundleHash $PreviousBundle | Out-Null
}

if (Test-Path -LiteralPath $Root) { Remove-Item -LiteralPath $Root -Recurse -Force }
$BundleRoot = Join-Path $Root "bundle"
$PreviousRoot = Join-Path $Root "previous-bundle"
$LocalAppData = Join-Path $Root "LocalAppData"
$UserProfile = Join-Path $Root "User"
$Temp = Join-Path $Root "Temp"
New-Item -ItemType Directory -Force -Path $BundleRoot, $LocalAppData, $UserProfile, $Temp | Out-Null
Expand-Archive -LiteralPath $Bundle -DestinationPath $BundleRoot -Force
if ($PreviousBundle) {
    New-Item -ItemType Directory -Force -Path $PreviousRoot | Out-Null
    Expand-Archive -LiteralPath $PreviousBundle -DestinationPath $PreviousRoot -Force
}

$env:LOCALAPPDATA = $LocalAppData
$env:APPDATA = Join-Path $Root "AppData"
$env:USERPROFILE = $UserProfile
$env:TEMP = $Temp
$env:TMP = $Temp
$env:IMPERIUM_HOME = Join-Path $LocalAppData "Imperium"
$env:A0_LMM_ROUTER_CONFIG = Join-Path $env:IMPERIUM_HOME "conf\llama_cpp_servers.yaml"
$env:PATH = "$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\WindowsPowerShell\v1.0"
if ($FullOfflineSetup) { $env:IMPERIUM_OFFLINE = "1" }
if ($Launch) { $KeepInstalled = $true }

$Python = ""
try {
if ($PreviousBundle) {
    & (Join-Path $PreviousRoot "Install-Imperium.ps1") -NoShortcut -NoLaunch
}
& (Join-Path $BundleRoot "Install-Imperium.ps1") -NoShortcut -NoLaunch
$Target = Join-Path $LocalAppData "Programs\Imperium"
$Python = Join-Path $Target "runtime\python\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { throw "The private Python runtime was not installed." }
$CandidateVersion = (& $Python -c "from local_model_router import __version__; print(__version__)").Trim()
$AgentCount = (& $Python -c "import yaml; from importlib.resources import files; from pydantic_ai.models.openai import OpenAIChatModel; from pydantic_ai.providers.openai import OpenAIProvider; print(len(yaml.safe_load(files('local_model_router.service').joinpath('agents.yaml').read_text(encoding='utf-8'))['agents']))").Trim()
if ($AgentCount -ne "4") { throw "The packaged Agent Library catalog is missing or incomplete." }

$DoctorRaw = (& $Python -m local_model_router doctor --json | Out-String)
$DoctorExit = $LASTEXITCODE
$Doctor = $DoctorRaw | ConvertFrom-Json
$DependencyFailures = @($Doctor.checks | Where-Object { $_.code -like "dependency_*" -and $_.status -ne "pass" })
if ($DependencyFailures.Count) { throw "The private runtime failed a required dependency capability check." }
$DoctorFailures = @($Doctor.checks | Where-Object { $_.status -eq "fail" })
$DoctorContract = $DoctorFailures.Count -eq 1 -and $DoctorFailures[0].code -eq "config_file_exists"
$Status = (& $Python -m local_model_router setup --status | ConvertFrom-Json)
if ($Status.home -ne $env:IMPERIUM_HOME) { throw "Setup state escaped the clean-room data directory." }
if ($Status.discovery.config_exists -or $Status.setup_complete) { throw "A clean installation was incorrectly reported as configured." }

$RepairVerified = $false
$UpdateChecked = $false
$RuntimeRollbackVerified = $false
$RuntimeTransition = $null
if ($FullOfflineSetup) {
    $Manifest = Get-Content -LiteralPath (Join-Path $Target "bundle-manifest.json") -Raw | ConvertFrom-Json
    if (-not $Manifest.offline_assets -or $Manifest.offline_assets.Count -lt 2) {
        throw "The selected bundle does not contain a runtime and model offline pack."
    }
    $PlanPath = Join-Path $Root "offline-plan.json"
    @{
        backend = $Backend
        model_id = "qwen3-1.7b-q8"
        runtime_channel = "recommended"
        launch_mode = "background"
        port = 18080
    } | ConvertTo-Json | Set-Content -LiteralPath $PlanPath -Encoding UTF8
    & $Python -m local_model_router setup --plan $PlanPath --yes | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "The full offline setup did not complete." }
    $Status = (& $Python -m local_model_router setup --status | ConvertFrom-Json)
    if (-not $Status.setup_complete) { throw "The full offline setup was not reported complete." }

    Remove-Item -LiteralPath $env:A0_LMM_ROUTER_CONFIG -Force
    & $Python -m local_model_router setup --repair --yes | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $env:A0_LMM_ROUTER_CONFIG)) {
        throw "Setup repair did not recover the managed configuration."
    }
    $RepairVerified = $true
    $Doctor = (& $Python -m local_model_router doctor --json | ConvertFrom-Json)
    if (-not $Doctor.ok) { throw "Doctor did not pass after full offline repair." }

    if ($Lifecycle) {
        & $Python -m local_model_router setup --stop-runtime | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "The managed runtime did not stop before update." }
        $OfflineMode = $env:IMPERIUM_OFFLINE
        Remove-Item Env:IMPERIUM_OFFLINE -ErrorAction SilentlyContinue
        try {
            $Update = (& $Python -m local_model_router update --check | ConvertFrom-Json)
            if ($LASTEXITCODE -ne 0) { throw "The managed runtime update check failed." }
            $UpdateChecked = $true
            if (-not $Update.update_available) {
                throw "Runtime rollback evidence requires an available verified update."
            }
            $Updated = (& $Python -m local_model_router update --yes | ConvertFrom-Json)
            if ($LASTEXITCODE -ne 0 -or $Updated.runtime.tag -ne $Update.latest) {
                throw "The managed runtime update failed."
            }
            $RolledBack = (& $Python -m local_model_router rollback | ConvertFrom-Json)
            if ($LASTEXITCODE -ne 0 -or $RolledBack.runtime.tag -ne $Update.installed) {
                throw "The managed runtime rollback failed."
            }
            $Reupdated = (& $Python -m local_model_router update --yes | ConvertFrom-Json)
            if ($LASTEXITCODE -ne 0 -or $Reupdated.runtime.tag -ne $Update.latest) {
                throw "The managed runtime could not return to the candidate update."
            }
            $RuntimeTransition = [ordered]@{
                from = $Update.installed
                to = $Update.latest
                rollback = $RolledBack.runtime.tag
                final = $Reupdated.runtime.tag
            }
        } finally {
            if ($OfflineMode) { $env:IMPERIUM_OFFLINE = $OfflineMode }
        }
        & $Python -m local_model_router setup --start-runtime | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "The updated managed runtime did not restart." }
        $RuntimeRollbackVerified = $true
    }
}

$env:OBSERVER_HOST = "127.0.0.1"
$env:OBSERVER_PORT = "9100"
$env:A0_LMM_ROUTER_AGENT_BASE_URL = "http://127.0.0.1:9100/v1"
$RouterOut = Join-Path $Root "router.stdout.log"
$RouterErr = Join-Path $Root "router.stderr.log"
$RouterProcess = Start-Process -FilePath $Python -ArgumentList "-m", "local_model_router", "serve" `
    -WorkingDirectory $Target -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $RouterOut -RedirectStandardError $RouterErr
$Health = $null
$UiStatus = $null
$Models = $null
$ProviderResult = Join-Path $Root "provider-smoke.json"
try {
    for ($Attempt = 0; $Attempt -lt 60; $Attempt++) {
        try {
            $Health = Invoke-RestMethod "http://127.0.0.1:9100/health" -TimeoutSec 2
            if ($Health.service -eq "lmm-router-observer") { break }
        } catch {}
        Start-Sleep -Milliseconds 250
    }
    if (-not $Health -or $Health.service -ne "lmm-router-observer") {
        throw "The installed router did not pass its identity health check."
    }
    $UiStatus = Invoke-RestMethod "http://127.0.0.1:9100/ui/status" -TimeoutSec 10
    if (-not $UiStatus.overall) { throw "The installed UI readiness endpoint is invalid." }
    $Models = Invoke-RestMethod "http://127.0.0.1:9100/v1/models" -TimeoutSec 30
    if ($null -eq $Models.data) { throw "The installed models endpoint is invalid." }
    $Agents = Invoke-RestMethod "http://127.0.0.1:9100/agents" -TimeoutSec 10
    if (@($Agents.agents).Count -ne 4) { throw "The installed /agents catalog is incomplete." }
    $SmokeArgs = @{ BaseUrl = "http://127.0.0.1:9100"; JsonOutput = $ProviderResult }
    if ($FullOfflineSetup) { $SmokeArgs.RequireLive = $true }
    & (Join-Path $Repo "scripts\smoke_provider.ps1") @SmokeArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "The installed provider smoke failed." }
    if ($FullOfflineSetup) {
        $AgentRun = Invoke-RestMethod "http://127.0.0.1:9100/agents/code-review/runs" -Method Post `
            -ContentType "application/json" -Body (@{ input = "Review: def add(a, b): return a + b" } | ConvertTo-Json) `
            -TimeoutSec 180
        if (-not $AgentRun.output) { throw "The offline Agent Library run returned no output." }
    }
} finally {
    if ($RouterProcess -and -not $RouterProcess.HasExited) {
        Stop-Process -Id $RouterProcess.Id -Force
        $RouterProcess.WaitForExit(10000) | Out-Null
    }
    if ($FullOfflineSetup -and (Test-Path -LiteralPath $Python)) {
        & $Python -m local_model_router setup --stop-runtime | Out-Null
    }
}

if ($FullOfflineSetup) {
    & $Python -m local_model_router setup --stop-runtime | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "The managed runtime did not stop cleanly." }
}

$ApplicationRollbackVerified = $false
if ($Lifecycle) {
    if (-not (Test-Path -LiteralPath (Join-Path $LocalAppData "Programs\Imperium.previous"))) {
        & (Join-Path $BundleRoot "Install-Imperium.ps1") -NoShortcut -NoLaunch
    }
    & (Join-Path $Target "Rollback-Imperium.ps1") | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "The packaged application rollback failed." }
    & (Join-Path $Target "Rollback-Imperium.ps1") | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "The packaged application restore failed." }
    $RestoredVersion = (& (Join-Path $Target "runtime\python\python.exe") -c "from local_model_router import __version__; print(__version__)").Trim()
    if ($RestoredVersion -ne $CandidateVersion) { throw "The packaged application did not return to the candidate version." }
    $ApplicationRollbackVerified = $true
}

$DataSentinel = Join-Path $env:IMPERIUM_HOME "state\cleanroom-preserve.txt"
$OutsideSentinel = Join-Path $Root "outside-preserve.txt"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $DataSentinel) | Out-Null
Set-Content -LiteralPath $DataSentinel -Value "preserve" -Encoding ASCII
Set-Content -LiteralPath $OutsideSentinel -Value "preserve" -Encoding ASCII
$UninstallVerified = $false
if (-not $KeepInstalled) {
    & (Join-Path $Target "Uninstall-Imperium.ps1") -NoShortcut | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "The packaged uninstall failed." }
    if (Test-Path -LiteralPath $Target) { throw "The packaged uninstall left the application directory behind." }
    if (-not (Test-Path -LiteralPath $DataSentinel) -or -not (Test-Path -LiteralPath $OutsideSentinel)) {
        throw "The packaged uninstall removed preserved data outside its fixed application paths."
    }
    $UninstallVerified = $true
}

$Provider = Get-Content -LiteralPath $ProviderResult -Raw | ConvertFrom-Json
$Result = [ordered]@{
    schema_version = 1
    kind = "windows_cleanroom"
    ok = $true
    bundle_name = [IO.Path]::GetFileName($Bundle)
    package_version = $CandidateVersion
    checksum = $CandidateHash
    backend = $Backend
    full_offline_setup = [bool]$FullOfflineSetup
    lifecycle = [bool]$Lifecycle
    runtime_transition = $(if ($Lifecycle) { $RuntimeTransition } else { $null })
    checks = [ordered]@{
        private_python = $true
        dependency_capabilities = ($DependencyFailures.Count -eq 0)
        doctor = [bool]$(if ($FullOfflineSetup) { $Doctor.ok } else { $DoctorContract })
        doctor_ready = [bool]$Doctor.ok
        health = ($Health.service -eq "lmm-router-observer")
        ui_status = [bool]$UiStatus.overall
        models = ($null -ne $Models.data)
        provider = [bool]$Provider.ok
        provider_live = [bool]$Provider.live
        repair = $(if ($FullOfflineSetup) { $RepairVerified } else { $null })
        update_check = $(if ($Lifecycle) { $UpdateChecked } else { $null })
        runtime_rollback = $(if ($Lifecycle) { $RuntimeRollbackVerified } else { $null })
        application_rollback = $(if ($Lifecycle) { $ApplicationRollbackVerified } else { $null })
        uninstall = $UninstallVerified
        preserved_data = ((Test-Path -LiteralPath $DataSentinel) -and (Test-Path -LiteralPath $OutsideSentinel))
    }
}
if (-not $ResultPath) { $ResultPath = Join-Path $Root "cleanroom-result.json" }
$Result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ResultPath -Encoding UTF8
$Result | ConvertTo-Json -Depth 8

if ($Launch) {
    Start-Process -FilePath (Join-Path $Target "START.bat") -WorkingDirectory $Target
}
} finally {
    if ($FullOfflineSetup -and $Python -and (Test-Path -LiteralPath $Python)) {
        & $Python -m local_model_router setup --stop-runtime | Out-Null
    }
}

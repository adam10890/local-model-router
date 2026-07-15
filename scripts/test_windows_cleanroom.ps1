param(
    [string]$Bundle = "",
    [string]$Root = "",
    [switch]$Launch,
    [switch]$FullOfflineSetup
)

$ErrorActionPreference = "Stop"
$Repo = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$BuildRoot = [IO.Path]::GetFullPath((Join-Path $Repo "build"))

function Test-StrictChildPath([string]$Parent, [string]$Child) {
    $Relative = [IO.Path]::GetRelativePath([IO.Path]::GetFullPath($Parent), [IO.Path]::GetFullPath($Child))
    return $Relative -ne "." -and -not [IO.Path]::IsPathRooted($Relative) -and
        $Relative -ne ".." -and -not $Relative.StartsWith("..$([IO.Path]::DirectorySeparatorChar)")
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
if (-not $Bundle -or -not (Test-Path -LiteralPath $Bundle)) {
    throw "Build the Windows bundle before running the clean-room test."
}
$Bundle = [IO.Path]::GetFullPath($Bundle)
$HashFile = "$Bundle.sha256"
if (-not (Test-Path -LiteralPath $HashFile)) { throw "The bundle SHA-256 sidecar is missing." }
$ExpectedHash = ((Get-Content -LiteralPath $HashFile -Raw).Trim() -split "\s+")[0].ToLowerInvariant()
$ActualHash = (Get-FileHash -LiteralPath $Bundle -Algorithm SHA256).Hash.ToLowerInvariant()
if ($ExpectedHash -ne $ActualHash) { throw "The bundle SHA-256 does not match its sidecar." }

if (Test-Path -LiteralPath $Root) { Remove-Item -LiteralPath $Root -Recurse -Force }
$BundleRoot = Join-Path $Root "bundle"
$LocalAppData = Join-Path $Root "LocalAppData"
$UserProfile = Join-Path $Root "User"
$Temp = Join-Path $Root "Temp"
New-Item -ItemType Directory -Force -Path $BundleRoot, $LocalAppData, $UserProfile, $Temp | Out-Null
Expand-Archive -LiteralPath $Bundle -DestinationPath $BundleRoot -Force

$env:LOCALAPPDATA = $LocalAppData
$env:APPDATA = Join-Path $Root "AppData"
$env:USERPROFILE = $UserProfile
$env:TEMP = $Temp
$env:TMP = $Temp
$env:IMPERIUM_HOME = Join-Path $LocalAppData "Imperium"
$env:A0_LMM_ROUTER_CONFIG = Join-Path $env:IMPERIUM_HOME "conf\llama_cpp_servers.yaml"
$env:PATH = "$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\WindowsPowerShell\v1.0"
if ($FullOfflineSetup) { $env:IMPERIUM_OFFLINE = "1" }

& (Join-Path $BundleRoot "Install-Imperium.ps1") -NoShortcut -NoLaunch
$Target = Join-Path $LocalAppData "Programs\Imperium"
$Python = Join-Path $Target "runtime\python\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { throw "The private Python runtime was not installed." }
$AgentCount = (& $Python -c "import yaml; from importlib.resources import files; from pydantic_ai.models.openai import OpenAIChatModel; from pydantic_ai.providers.openai import OpenAIProvider; print(len(yaml.safe_load(files('local_model_router.service').joinpath('agents.yaml').read_text(encoding='utf-8'))['agents']))").Trim()
if ($AgentCount -ne "4") { throw "The packaged Agent Library catalog is missing or incomplete." }

$Status = (& $Python -m local_model_router setup --status | ConvertFrom-Json)
if ($Status.home -ne $env:IMPERIUM_HOME) { throw "Setup state escaped the clean-room data directory." }
if ($Status.discovery.config_exists -or $Status.setup_complete) { throw "A clean installation was incorrectly reported as configured." }

if ($FullOfflineSetup) {
    $Manifest = Get-Content -LiteralPath (Join-Path $Target "bundle-manifest.json") -Raw | ConvertFrom-Json
    if (-not $Manifest.offline_assets -or $Manifest.offline_assets.Count -lt 2) {
        throw "The selected bundle does not contain a runtime and model offline pack."
    }
    $PlanPath = Join-Path $Root "offline-plan.json"
    @{
        backend = "cpu"
        model_id = "qwen3-1.7b-q8"
        runtime_channel = "recommended"
        launch_mode = "background"
        port = 18080
    } | ConvertTo-Json | Set-Content -LiteralPath $PlanPath -Encoding UTF8
    & $Python -m local_model_router setup --plan $PlanPath --yes
    if ($LASTEXITCODE -ne 0) { throw "The full offline setup did not complete." }
    $Status = (& $Python -m local_model_router setup --status | ConvertFrom-Json)
    if (-not $Status.setup_complete) { throw "The full offline setup was not reported complete." }
}

$env:OBSERVER_HOST = "127.0.0.1"
$env:OBSERVER_PORT = "9100"
$env:A0_LMM_ROUTER_AGENT_BASE_URL = "http://127.0.0.1:9100/v1"
$RouterOut = Join-Path $Root "router.stdout.log"
$RouterErr = Join-Path $Root "router.stderr.log"
$RouterProcess = Start-Process -FilePath $Python -ArgumentList "-m", "local_model_router", "serve" `
    -WorkingDirectory $Target -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $RouterOut -RedirectStandardError $RouterErr
try {
    $Health = $null
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
    $Agents = Invoke-RestMethod "http://127.0.0.1:9100/agents" -TimeoutSec 10
    if (@($Agents.agents).Count -ne 4) { throw "The installed /agents catalog is incomplete." }
    $UiStatus = Invoke-RestMethod "http://127.0.0.1:9100/ui/status" -TimeoutSec 10
    if (-not $UiStatus.overall) { throw "The installed UI readiness endpoint is invalid." }
    if ($FullOfflineSetup) {
        $Models = Invoke-RestMethod "http://127.0.0.1:9100/v1/models" -TimeoutSec 30
        if (-not @($Models.data).Count) { throw "The offline router exposes no models." }
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
}

$Result = [ordered]@{
    ok = $true
    bundle = [IO.Path]::GetFullPath($Bundle)
    root = $Root
    installed = $Target
    private_python = $Python
    config_exists = $Status.discovery.config_exists
    setup_complete = $Status.setup_complete
    recommendation = $Status.recommendation.id
    agents = [int]$AgentCount
    router_health = $Health.service
    checksum = $ActualHash
    full_offline_setup = [bool]$FullOfflineSetup
}
$Result | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Root "cleanroom-result.json") -Encoding UTF8
$Result | ConvertTo-Json

if ($FullOfflineSetup -and -not $Launch) {
    & $Python -m local_model_router setup --stop-runtime | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "The managed runtime did not stop cleanly." }
}

if ($Launch) {
    Start-Process -FilePath (Join-Path $Target "START.bat") -WorkingDirectory $Target
}

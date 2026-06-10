[CmdletBinding()]
param(
    [string]$HostName = $(if ($env:OBSERVER_HOST) { $env:OBSERVER_HOST } else { "127.0.0.1" }),
    [int]$Port = $(if ($env:OBSERVER_PORT) { [int]$env:OBSERVER_PORT } else { 9000 }),
    [string]$ConfigPath = $env:A0_LMM_ROUTER_CONFIG,
    [string]$ApiKey = $env:A0_LMM_ROUTER_API_KEY,
    [switch]$AllowPublicNoAuth,
    [switch]$InstallDeps,
    [string]$Python = $(if ($env:PYTHON) { $env:PYTHON } else { "python" })
)

$ErrorActionPreference = "Stop"

$PluginRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
if (-not $ConfigPath) {
    $ConfigPath = Join-Path $PluginRoot "conf\llama_cpp_servers.yaml"
}

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Config file not found: $ConfigPath"
}

$env:OBSERVER_HOST = $HostName
$env:OBSERVER_PORT = [string]$Port
$env:A0_LMM_ROUTER_CONFIG = (Resolve-Path -LiteralPath $ConfigPath).Path

if ($ApiKey) {
    $env:A0_LMM_ROUTER_API_KEY = $ApiKey
}
if ($AllowPublicNoAuth) {
    $env:A0_LMM_ROUTER_ALLOW_PUBLIC_NO_AUTH = "1"
}

if ($InstallDeps) {
    & $Python -m pip install -e $PluginRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed."
    }
}

Write-Host "Starting local-model-router"
Write-Host "  Bind:   http://$($env:OBSERVER_HOST):$($env:OBSERVER_PORT)"
Write-Host "  Config: $($env:A0_LMM_ROUTER_CONFIG)"
Write-Host "  Auth:   $(if ($env:A0_LMM_ROUTER_API_KEY) { 'Bearer token required' } else { 'dev/no key' })"

Push-Location $PluginRoot
try {
    & $Python -m local_model_router.service
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}

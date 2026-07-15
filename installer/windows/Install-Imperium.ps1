param(
    [switch]$NoShortcut,
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"

$Source = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if (-not (Test-Path (Join-Path $Source "bundle-manifest.json"))) {
    $Source = [IO.Path]::GetFullPath($PSScriptRoot)
}
$Programs = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Programs"))
$Target = [IO.Path]::GetFullPath((Join-Path $Programs "Imperium"))
$Previous = [IO.Path]::GetFullPath((Join-Path $Programs "Imperium.previous"))
$Staging = [IO.Path]::GetFullPath((Join-Path $Programs ("Imperium.staging-" + [guid]::NewGuid().ToString("N"))))

foreach ($Path in @($Target, $Previous, $Staging)) {
    if (-not $Path.StartsWith($Programs, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to install outside the current user's Programs directory."
    }
}
if (-not (Test-Path (Join-Path $Source "bundle-manifest.json"))) {
    throw "This folder is not a complete Imperium release bundle."
}

New-Item -ItemType Directory -Force -Path $Programs | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $env:LOCALAPPDATA "Imperium") | Out-Null
New-Item -ItemType Directory -Force -Path $Staging | Out-Null
Copy-Item -Path (Join-Path $Source "*") -Destination $Staging -Recurse -Force

$Python = Join-Path $Staging "runtime\python\python.exe"
if (-not (Test-Path $Python)) { throw "The private Python runtime is missing from the bundle." }
& $Python -m local_model_router --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Imperium failed its installation smoke test." }
$BundleManifest = Get-Content -LiteralPath (Join-Path $Staging "bundle-manifest.json") -Raw | ConvertFrom-Json
$PackageVersion = (& $Python -c "from local_model_router import __version__; print(__version__)").Trim()
if (-not $BundleManifest.version -or $BundleManifest.version -ne $PackageVersion) {
    throw "Bundle manifest version does not match the packaged Imperium version."
}

try {
    if (Test-Path $Previous) { Remove-Item -LiteralPath $Previous -Recurse -Force }
    if (Test-Path $Target) { Move-Item -LiteralPath $Target -Destination $Previous }
    Move-Item -LiteralPath $Staging -Destination $Target
} catch {
    if ((-not (Test-Path $Target)) -and (Test-Path $Previous)) {
        Move-Item -LiteralPath $Previous -Destination $Target
    }
    throw
}

if (-not $NoShortcut) {
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "Imperium.lnk"))
    $Shortcut.TargetPath = Join-Path $Target "START.bat"
    $Shortcut.WorkingDirectory = $Target
    $Shortcut.Description = "Imperium local model router"
    $Shortcut.Save()
}

if (-not $NoLaunch) {
    Start-Process -FilePath (Join-Path $Target "START.bat") -WorkingDirectory $Target
}
Write-Output "Imperium was installed for the current user at $Target"

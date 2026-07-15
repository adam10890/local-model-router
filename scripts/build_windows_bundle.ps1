param(
    [string]$Version = "",
    [string]$PythonVersion = "3.14.6",
    [string]$PythonSha256 = "df901e84a896ff1ee720ad03377e0c8d8c2244fda79808aeeaff6316df1cb75c",
    [string]$HostPython = "",
    [string]$DependencySeed = "",
    [switch]$IncludeOfflineAssets,
    [string[]]$OfflineBackends = @("cpu", "vulkan", "cuda12")
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Build = Join-Path $Root "build\windows"
$WheelDir = Join-Path $Build "wheel"
$Archive = Join-Path $Build "python-$PythonVersion-embed-amd64.zip"
$Download = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$OfflineAssets = @()

function Test-StrictChildPath([string]$Parent, [string]$Child) {
    $Relative = [IO.Path]::GetRelativePath([IO.Path]::GetFullPath($Parent), [IO.Path]::GetFullPath($Child))
    return $Relative -ne "." -and -not [IO.Path]::IsPathRooted($Relative) -and
        $Relative -ne ".." -and -not $Relative.StartsWith("..$([IO.Path]::DirectorySeparatorChar)")
}

if (-not $HostPython) {
    $WorkspacePython = Join-Path $Root ".venv\Scripts\python.exe"
    $HostPython = if (Test-Path $WorkspacePython) { $WorkspacePython } else { "python" }
}
& $HostPython -m pip --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "A host Python with pip is required to assemble the bundle." }

Push-Location $Root
try {
    $ProjectVersion = (& $HostPython -c "from local_model_router import __version__; print(__version__)").Trim()
} finally {
    Pop-Location
}
if (-not $ProjectVersion) { throw "Could not read the Imperium package version." }
if ($Version -and $Version -ne $ProjectVersion) {
    throw "Requested bundle version '$Version' does not match package version '$ProjectVersion'."
}
$Version = $ProjectVersion
$Stage = Join-Path $Build ".stage-Imperium-$Version-$PID"
$Runtime = Join-Path $Stage "runtime\python"

if (Test-Path $Stage) {
    if (-not (Test-StrictChildPath $Build $Stage)) {
        throw "Refusing to clean a staging path outside the build directory."
    }
    Remove-Item -LiteralPath $Stage -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
if (-not (Test-Path $Archive)) {
    Invoke-WebRequest -Uri $Download -OutFile $Archive -UseBasicParsing
}
if ((Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $PythonSha256.ToLowerInvariant()) {
    throw "Python runtime checksum verification failed."
}
Expand-Archive -LiteralPath $Archive -DestinationPath $Runtime -Force

$Pth = Get-ChildItem -LiteralPath $Runtime -Filter "python*._pth" | Select-Object -First 1
@("python314.zip", ".", "Lib\site-packages", "import site") | Set-Content -LiteralPath $Pth.FullName -Encoding ASCII
New-Item -ItemType Directory -Force -Path (Join-Path $Runtime "Lib\site-packages") | Out-Null

if (Test-Path $WheelDir) { Remove-Item -LiteralPath $WheelDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $WheelDir | Out-Null
& $HostPython -m pip wheel --disable-pip-version-check --no-build-isolation --no-deps --wheel-dir $WheelDir $Root
if ($LASTEXITCODE -ne 0) { throw "Application wheel build failed." }
$AppWheel = Get-ChildItem -LiteralPath $WheelDir -Filter "local_model_router-*.whl" | Select-Object -First 1
if (-not $AppWheel) { throw "The application wheel was not created." }
$AppRequirement = "$($AppWheel.FullName)[agents]"
$TargetPackages = Join-Path $Runtime "Lib\site-packages"
if ($DependencySeed) {
    $DependencySeed = [IO.Path]::GetFullPath($DependencySeed)
    if (-not (Test-Path -LiteralPath $DependencySeed -PathType Container)) {
        throw "The dependency seed must be an existing site-packages directory."
    }
    Get-ChildItem -LiteralPath $DependencySeed -Force |
        Where-Object { $_.Name -notlike "local_model_router*" -and $_.Name -ne "bin" } |
        Copy-Item -Destination $TargetPackages -Recurse -Force
    & $HostPython -m pip --python (Join-Path $Runtime "python.exe") install --disable-pip-version-check --no-compile --no-deps --target $TargetPackages $AppWheel.FullName
} else {
    & $HostPython -m pip --python (Join-Path $Runtime "python.exe") install --disable-pip-version-check --no-compile --only-binary=:all: --target $TargetPackages $AppRequirement
}
if ($LASTEXITCODE -ne 0) { throw "Application dependency packaging failed." }
& (Join-Path $Runtime "python.exe") -c "from pydantic_ai.models.openai import OpenAIChatModel; from pydantic_ai.providers.openai import OpenAIProvider; from local_model_router.service.agent_library import AgentCatalog; assert len(AgentCatalog.load_packaged().public_list()) == 4"
if ($LASTEXITCODE -ne 0) { throw "The Agent Library runtime was not packaged correctly." }

if (-not (Test-StrictChildPath $Stage $TargetPackages)) {
    throw "Refusing to clean package metadata outside the staging directory."
}
$PackageScripts = Join-Path $TargetPackages "bin"
if (Test-Path -LiteralPath $PackageScripts) {
    if (-not (Test-StrictChildPath $TargetPackages $PackageScripts)) { throw "Invalid package scripts path." }
    Remove-Item -LiteralPath $PackageScripts -Recurse -Force
}
Get-ChildItem -LiteralPath $TargetPackages -Recurse -Directory -Filter "__pycache__" |
    Sort-Object { $_.FullName.Length } -Descending |
    ForEach-Object {
        if (-not (Test-StrictChildPath $TargetPackages $_.FullName)) { throw "Invalid bytecode cache path." }
        Remove-Item -LiteralPath $_.FullName -Recurse -Force
    }
Get-ChildItem -LiteralPath $TargetPackages -Recurse -File -Filter "*.pyc" |
    ForEach-Object {
        if (-not (Test-StrictChildPath $TargetPackages $_.FullName)) { throw "Invalid bytecode path." }
        Remove-Item -LiteralPath $_.FullName -Force
    }
Get-ChildItem -LiteralPath $TargetPackages -Recurse -File -Filter "direct_url.json" |
    ForEach-Object {
        if (-not (Test-StrictChildPath $TargetPackages $_.FullName)) { throw "Invalid install metadata path." }
        Remove-Item -LiteralPath $_.FullName -Force
    }
Get-ChildItem -LiteralPath $TargetPackages -Recurse -File -Filter "RECORD" |
    ForEach-Object {
        $Rows = Get-Content -LiteralPath $_.FullName | Where-Object { $_ -notmatch "direct_url\.json," }
        $Rows | Set-Content -LiteralPath $_.FullName -Encoding UTF8
    }

Copy-Item -LiteralPath (Join-Path $Root "START.bat") -Destination $Stage
Copy-Item -LiteralPath (Join-Path $Root "STOP.bat") -Destination $Stage
$License = Join-Path $Root "LICENSE"
$Notices = Join-Path $Root "THIRD_PARTY_NOTICES.md"
if (-not (Test-Path $License)) { throw "The project LICENSE file is missing." }
if (-not (Test-Path $Notices)) { throw "The third-party notices file is missing." }
Copy-Item -LiteralPath $License -Destination $Stage
Copy-Item -LiteralPath $Notices -Destination $Stage
Copy-Item -LiteralPath (Join-Path $Root "licenses") -Destination $Stage -Recurse
Copy-Item -LiteralPath (Join-Path $Root "installer\windows\Install-Imperium.ps1") -Destination $Stage
Copy-Item -LiteralPath (Join-Path $Root "installer\windows\Install-Imperium.bat") -Destination $Stage
Copy-Item -LiteralPath (Join-Path $Root "installer\windows\Rollback-Imperium.ps1") -Destination $Stage
Copy-Item -LiteralPath (Join-Path $Root "installer\windows\Rollback-Imperium.bat") -Destination $Stage
Copy-Item -LiteralPath (Join-Path $Root "installer\windows\Uninstall-Imperium.ps1") -Destination $Stage

if ($IncludeOfflineAssets) {
    $Offline = Join-Path $Stage "offline"
    $Cache = Join-Path $Build "offline-cache"
    New-Item -ItemType Directory -Force -Path $Offline, $Cache | Out-Null
    $RuntimeCatalog = Get-Content -LiteralPath (Join-Path $Root "local_model_router\setup\runtime_catalog.json") -Raw | ConvertFrom-Json
    $Platform = $RuntimeCatalog.platforms | Where-Object { $_.os -eq "windows" -and $_.arch -eq "x86_64" } | Select-Object -First 1
    $ModelCatalog = Get-Content -LiteralPath (Join-Path $Root "local_model_router\setup\model_catalog.json") -Raw | ConvertFrom-Json
    $Assets = @()
    foreach ($Backend in $OfflineBackends) {
        $BackendEntry = $Platform.backends.$Backend
        if (-not $BackendEntry -or $BackendEntry.status -ne "supported") { throw "Offline backend is not supported: $Backend" }
        $Assets += @($BackendEntry.assets)
    }
    $Model = $ModelCatalog.models | Where-Object { $_.first_run_default } | Select-Object -First 1
    $Assets += [pscustomobject]@{ name = $Model.filename; url = $Model.download_url; sha256 = $Model.sha256; size_bytes = $Model.size_bytes }
    foreach ($Asset in ($Assets | Sort-Object name -Unique)) {
        $Cached = Join-Path $Cache $Asset.name
        $Verified = (Test-Path -LiteralPath $Cached) -and
            ((Get-FileHash -LiteralPath $Cached -Algorithm SHA256).Hash.ToLowerInvariant() -eq $Asset.sha256.ToLowerInvariant())
        if (-not $Verified) {
            & curl.exe --location --fail --retry 5 --retry-all-errors --retry-delay 2 --continue-at - --output $Cached $Asset.url
            if ($LASTEXITCODE -ne 0) { throw "Offline asset download failed: $($Asset.name)" }
        }
        if ((Get-FileHash -LiteralPath $Cached -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Asset.sha256.ToLowerInvariant()) {
            Remove-Item -LiteralPath $Cached -Force
            throw "Offline asset checksum verification failed: $($Asset.name)"
        }
        Copy-Item -LiteralPath $Cached -Destination $Offline
        $OfflineAssets += $Asset.name
    }
}

$Manifest = @{
    schema_version = 2
    product = "Imperium"
    version = $Version
    python = $PythonVersion
    offline_assets = $OfflineAssets
    built_at = (Get-Date).ToUniversalTime().ToString("o")
} | ConvertTo-Json
$Manifest | Set-Content -LiteralPath (Join-Path $Stage "bundle-manifest.json") -Encoding UTF8

$Zip = Join-Path $Build "Imperium-$Version-windows-x64.zip"
if (Test-Path $Zip) { Remove-Item -LiteralPath $Zip -Force }
if ($IncludeOfflineAssets) {
    & $HostPython (Join-Path $PSScriptRoot "zip_tree.py") $Stage $Zip
    if ($LASTEXITCODE -ne 0) { throw "The streaming offline archive build failed." }
} else {
    Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $Zip -CompressionLevel Optimal
}
$Hash = (Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash.ToLowerInvariant()
$HashFile = "$Zip.sha256"
"$Hash  $([IO.Path]::GetFileName($Zip))" | Set-Content -LiteralPath $HashFile -Encoding ASCII
Write-Output $Zip
Write-Output $HashFile

param(
    [switch]$NoShortcut,
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"

function Test-StrictChildPath([string]$Parent, [string]$Child) {
    $Sep = [IO.Path]::DirectorySeparatorChar
    $FullParent = [IO.Path]::GetFullPath($Parent).TrimEnd($Sep, [IO.Path]::AltDirectorySeparatorChar) + $Sep
    $FullChild = [IO.Path]::GetFullPath($Child).TrimEnd($Sep, [IO.Path]::AltDirectorySeparatorChar)
    $Cmp = $(if ($env:OS -eq "Windows_NT") { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal })
    return $FullChild.Length -gt $FullParent.Length -and $FullChild.StartsWith($FullParent, $Cmp)
}

$Source = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if (-not (Test-Path (Join-Path $Source "bundle-manifest.json"))) {
    $Source = [IO.Path]::GetFullPath($PSScriptRoot)
}
$Programs = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Programs"))
$Target = [IO.Path]::GetFullPath((Join-Path $Programs "Imperium"))
$Previous = [IO.Path]::GetFullPath((Join-Path $Programs "Imperium.previous"))
$RunId = [guid]::NewGuid().ToString("N")
$Staging = [IO.Path]::GetFullPath((Join-Path $Programs ("Imperium.staging-" + $RunId)))
$RetiredPrevious = [IO.Path]::GetFullPath((Join-Path $Programs ("Imperium.previous.retired-" + $RunId)))

foreach ($Path in @($Target, $Previous, $Staging, $RetiredPrevious)) {
    if (-not (Test-StrictChildPath $Programs $Path)) {
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
    if ((Test-Path -LiteralPath $Target) -and (Test-Path -LiteralPath $Previous)) {
        Move-Item -LiteralPath $Previous -Destination $RetiredPrevious
    }
    if (Test-Path -LiteralPath $Target) {
        Move-Item -LiteralPath $Target -Destination $Previous
    }
    if ($env:IMPERIUM_TEST_FAIL_PROMOTE -eq "1") {
        throw "Simulated promotion failure."
    }
    Move-Item -LiteralPath $Staging -Destination $Target
    if (Test-Path -LiteralPath $RetiredPrevious) {
        Remove-Item -LiteralPath $RetiredPrevious -Recurse -Force
    }
} catch {
    $PromotionError = $_
    if (Test-Path -LiteralPath $Staging) {
        Remove-Item -LiteralPath $Staging -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ((-not (Test-Path -LiteralPath $Target)) -and (Test-Path -LiteralPath $Previous)) {
        try {
            if ($env:IMPERIUM_TEST_FAIL_ROLLBACK -eq "1") {
                throw "Simulated rollback failure."
            }
            Move-Item -LiteralPath $Previous -Destination $Target
            if (Test-Path -LiteralPath $RetiredPrevious) {
                Move-Item -LiteralPath $RetiredPrevious -Destination $Previous
            }
        } catch {
            if ((Test-Path -LiteralPath $Previous) -and (Test-Path -LiteralPath $RetiredPrevious)) {
                Remove-Item -LiteralPath $RetiredPrevious -Recurse -Force -ErrorAction SilentlyContinue
            }
            Write-Output "Recoverable backup preserved at: $Previous"
            throw "Promotion failed ($($PromotionError.Exception.Message)) and automatic rollback failed ($($_.Exception.Message)). Recoverable backup preserved at: $Previous"
        }
    } elseif (Test-Path -LiteralPath $RetiredPrevious) {
        if (-not (Test-Path -LiteralPath $Previous)) {
            Move-Item -LiteralPath $RetiredPrevious -Destination $Previous -ErrorAction SilentlyContinue
        } else {
            Remove-Item -LiteralPath $RetiredPrevious -Recurse -Force -ErrorAction SilentlyContinue
        }
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

$ErrorActionPreference = "Stop"

function Test-StrictChildPath([string]$Parent, [string]$Child) {
    $Relative = [IO.Path]::GetRelativePath([IO.Path]::GetFullPath($Parent), [IO.Path]::GetFullPath($Child))
    return $Relative -ne "." -and -not [IO.Path]::IsPathRooted($Relative) -and
        $Relative -ne ".." -and -not $Relative.StartsWith("..$([IO.Path]::DirectorySeparatorChar)")
}

$Programs = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Programs"))
$Target = [IO.Path]::GetFullPath((Join-Path $Programs "Imperium"))
$Previous = [IO.Path]::GetFullPath((Join-Path $Programs "Imperium.previous"))
$Swap = [IO.Path]::GetFullPath((Join-Path $Programs ("Imperium.rollback-" + [guid]::NewGuid().ToString("N"))))
foreach ($Path in @($Target, $Previous, $Swap)) {
    if (-not (Test-StrictChildPath $Programs $Path)) {
        throw "Refusing to roll back outside the current user's Programs directory."
    }
}

$PreviousPython = Join-Path $Previous "runtime\python\python.exe"
if (-not (Test-Path -LiteralPath $PreviousPython)) {
    throw "No verified previous Imperium installation is available."
}
& $PreviousPython -m local_model_router --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "The previous Imperium installation failed validation." }

$CurrentPython = Join-Path $Target "runtime\python\python.exe"
if (Test-Path -LiteralPath $CurrentPython) {
    & $CurrentPython -m local_model_router setup --stop-runtime | Out-Null
}
& taskkill.exe /F /T /FI "WINDOWTITLE eq Imperium - Local Model Router" 2>$null | Out-Null
Set-Location -LiteralPath $Programs

try {
    if (Test-Path -LiteralPath $Target) { Move-Item -LiteralPath $Target -Destination $Swap }
    Move-Item -LiteralPath $Previous -Destination $Target
    if (Test-Path -LiteralPath $Swap) { Move-Item -LiteralPath $Swap -Destination $Previous }
} catch {
    if (-not (Test-Path -LiteralPath $Target)) {
        if (Test-Path -LiteralPath $Swap) {
            Move-Item -LiteralPath $Swap -Destination $Target
        } elseif (Test-Path -LiteralPath $Previous) {
            Move-Item -LiteralPath $Previous -Destination $Target
        }
    }
    if ((Test-Path -LiteralPath $Swap) -and -not (Test-Path -LiteralPath $Previous)) {
        Move-Item -LiteralPath $Swap -Destination $Previous
    }
    throw
}

$Version = (& (Join-Path $Target "runtime\python\python.exe") -c "from local_model_router import __version__; print(__version__)").Trim()
Write-Output "Imperium was rolled back to $Version. Run START.bat to launch it."

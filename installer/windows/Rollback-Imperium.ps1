$ErrorActionPreference = "Stop"

function Test-StrictChildPath([string]$Parent, [string]$Child) {
    $Sep = [IO.Path]::DirectorySeparatorChar
    $FullParent = [IO.Path]::GetFullPath($Parent).TrimEnd($Sep, [IO.Path]::AltDirectorySeparatorChar) + $Sep
    $FullChild = [IO.Path]::GetFullPath($Child).TrimEnd($Sep, [IO.Path]::AltDirectorySeparatorChar)
    $Cmp = $(if ($env:OS -eq "Windows_NT") { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal })
    return $FullChild.Length -gt $FullParent.Length -and $FullChild.StartsWith($FullParent, $Cmp)
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
$Stop = Join-Path $Target "STOP.bat"
if (Test-Path -LiteralPath $Stop) {
    & $Stop
    if ($LASTEXITCODE -ne 0) { throw "The running Imperium process could not be stopped safely." }
}
Set-Location -LiteralPath $Programs

try {
    if (Test-Path -LiteralPath $Target) { Move-Item -LiteralPath $Target -Destination $Swap }
    if ($env:IMPERIUM_TEST_FAIL_ROLLBACK -eq "1") {
        throw "Simulated rollback failure."
    }
    Move-Item -LiteralPath $Previous -Destination $Target
    if (Test-Path -LiteralPath $Swap) { Move-Item -LiteralPath $Swap -Destination $Previous }
} catch {
    $RollbackError = $_
    try {
        if (-not (Test-Path -LiteralPath $Target)) {
            if ($env:IMPERIUM_TEST_FAIL_ROLLBACK_RESTORE -eq "1") {
                throw "Simulated rollback restore failure."
            }
            if (Test-Path -LiteralPath $Swap) {
                Move-Item -LiteralPath $Swap -Destination $Target
            } elseif (Test-Path -LiteralPath $Previous) {
                Move-Item -LiteralPath $Previous -Destination $Target
            }
        }
        if ((Test-Path -LiteralPath $Swap) -and -not (Test-Path -LiteralPath $Previous)) {
            Move-Item -LiteralPath $Swap -Destination $Previous
        }
    } catch {
        $Surviving = $(if (Test-Path -LiteralPath $Previous) { $Previous } elseif (Test-Path -LiteralPath $Swap) { $Swap } else { $Programs })
        Write-Output "Recoverable backup preserved at: $Surviving"
        throw "Rollback failed ($($RollbackError.Exception.Message)) and restore failed ($($_.Exception.Message)). Recoverable backup preserved at: $Surviving"
    }
    throw
}

$Version = (& (Join-Path $Target "runtime\python\python.exe") -c "from local_model_router import __version__; print(__version__)").Trim()
Write-Output "Imperium was rolled back to $Version. Run START.bat to launch it."

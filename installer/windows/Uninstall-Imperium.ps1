$ErrorActionPreference = "Stop"

function Test-StrictChildPath([string]$Parent, [string]$Child) {
    $Relative = [IO.Path]::GetRelativePath([IO.Path]::GetFullPath($Parent), [IO.Path]::GetFullPath($Child))
    return $Relative -ne "." -and -not [IO.Path]::IsPathRooted($Relative) -and
        $Relative -ne ".." -and -not $Relative.StartsWith("..$([IO.Path]::DirectorySeparatorChar)")
}

$Programs = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Programs"))
$Target = [IO.Path]::GetFullPath((Join-Path $Programs "Imperium"))
$Previous = [IO.Path]::GetFullPath((Join-Path $Programs "Imperium.previous"))
foreach ($Path in @($Target, $Previous)) {
    if (-not (Test-StrictChildPath $Programs $Path)) {
        throw "Refusing to uninstall outside the current user's Programs directory."
    }
}

$Python = Join-Path $Target "runtime\python\python.exe"
if (Test-Path -LiteralPath $Python) {
    & $Python -m local_model_router setup --stop-runtime | Out-Null
}
$Stop = Join-Path $Target "STOP.bat"
if (Test-Path -LiteralPath $Stop) {
    & $Stop
    if ($LASTEXITCODE -ne 0) { throw "The running Imperium process could not be stopped safely." }
}

if (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target -Recurse -Force }
if (Test-Path -LiteralPath $Previous) { Remove-Item -LiteralPath $Previous -Recurse -Force }

$Shortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Imperium.lnk"
if (Test-Path -LiteralPath $Shortcut) { Remove-Item -LiteralPath $Shortcut -Force }
Write-Output "Imperium was removed. Models and settings remain under %LOCALAPPDATA%\Imperium."

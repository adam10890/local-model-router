$ErrorActionPreference = "Stop"
$Programs = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Programs"))
$Target = [IO.Path]::GetFullPath((Join-Path $Programs "Imperium"))
$Previous = [IO.Path]::GetFullPath((Join-Path $Programs "Imperium.previous"))
if (-not $Target.StartsWith($Programs, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to uninstall outside the current user's Programs directory."
}

$Python = Join-Path $Target "runtime\python\python.exe"
if (Test-Path $Python) {
    & $Python -m local_model_router setup --stop-runtime | Out-Null
}
if (Test-Path $Target) { Remove-Item -LiteralPath $Target -Recurse -Force }
if (Test-Path $Previous) { Remove-Item -LiteralPath $Previous -Recurse -Force }

$Shortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Imperium.lnk"
if (Test-Path $Shortcut) { Remove-Item -LiteralPath $Shortcut -Force }
Write-Output "Imperium was removed. Models and settings remain under %LOCALAPPDATA%\Imperium."

param(
    [switch]$OneFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$mode = if ($OneFile) { "onefile" } else { "onedir" }
$distPath = Join-Path "dist" $mode
$workPath = Join-Path "build" $mode

$arguments = @(
    ".\build.spec",
    "--noconfirm",
    "--distpath", $distPath,
    "--workpath", $workPath
)
$previousMode = $env:REDLOTUS_PYINSTALLER_MODE
$env:REDLOTUS_PYINSTALLER_MODE = $mode
try {
    uv run --extra build --extra browser pyinstaller @arguments
}
finally {
    $env:REDLOTUS_PYINSTALLER_MODE = $previousMode
}

param(
    [switch]$OneFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $root

$mode = if ($OneFile) { "onefile" } else { "onedir" }
$runtimeRoot = Join-Path $root "WorkDatabase\runtime\packaging"
$distPath = Join-Path $runtimeRoot "dist\$mode"
$workPath = Join-Path $runtimeRoot "build\$mode"
$cachePath = Join-Path $runtimeRoot "pyinstaller-cache"
$tempPath = Join-Path $runtimeRoot "tmp"
$venvPath = Join-Path $runtimeRoot "venv"

foreach ($path in @($distPath, $workPath, $cachePath, $tempPath)) {
    New-Item -ItemType Directory -Force -Path $path | Out-Null
}

$arguments = @(
    ".\build.spec",
    "--noconfirm",
    "--distpath", $distPath,
    "--workpath", $workPath
)
$previousMode = $env:REDLOTUS_PYINSTALLER_MODE
$previousEnvironment = @{}
foreach ($name in @("UV_CACHE_DIR", "UV_PROJECT_ENVIRONMENT", "UV_LINK_MODE", "UV_NO_MANAGED_PYTHON", "UV_PYTHON_DOWNLOADS", "PYINSTALLER_CONFIG_DIR", "TEMP", "TMP")) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}
$env:REDLOTUS_PYINSTALLER_MODE = $mode
$env:UV_CACHE_DIR = Join-Path $runtimeRoot "uv-cache"
$env:UV_PROJECT_ENVIRONMENT = $venvPath
$env:UV_LINK_MODE = "copy"
$env:UV_NO_MANAGED_PYTHON = "1"
$env:UV_PYTHON_DOWNLOADS = "never"
$env:PYINSTALLER_CONFIG_DIR = $cachePath
$env:TEMP = $tempPath
$env:TMP = $tempPath
$buildExitCode = 0
try {
    uv run --extra build --extra browser pyinstaller @arguments
    $buildExitCode = $LASTEXITCODE
}
finally {
    $env:REDLOTUS_PYINSTALLER_MODE = $previousMode
    foreach ($name in $previousEnvironment.Keys) {
        if ($null -eq $previousEnvironment[$name]) {
            Remove-Item "Env:$name" -ErrorAction SilentlyContinue
        }
        else {
            Set-Item "Env:$name" $previousEnvironment[$name]
        }
    }
}

if ($buildExitCode -ne 0) {
    exit $buildExitCode
}

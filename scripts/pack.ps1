param(
    [switch]$OneFile,
    [string]$Python
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
if (-not $Python) {
    $projectPython = Join-Path $root ".venv\Scripts\python.exe"
    $Python = if (Test-Path -LiteralPath $projectPython -PathType Leaf) {
        $projectPython
    } else {
        (Get-Command python -ErrorAction Stop).Source
    }
}
$Python = (Get-Command $Python -ErrorAction Stop).Source
& $Python -c "import PyInstaller; print('Build interpreter:', __import__('sys').executable)"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is missing from the selected existing environment: $Python"
}

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
foreach ($name in @("PYINSTALLER_CONFIG_DIR", "TEMP", "TMP")) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}
$env:REDLOTUS_PYINSTALLER_MODE = $mode
$env:PYINSTALLER_CONFIG_DIR = $cachePath
$env:TEMP = $tempPath
$env:TMP = $tempPath
$buildExitCode = 0
try {
    & $Python -m PyInstaller @arguments
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

<#
.SYNOPSIS
    将已验收、已冻结 SHA256 的 wheel 和 sdist 发布到 PyPI。
.EXAMPLE
    .\scripts\publish.ps1 -Version 1.0.1.post1 -ArtifactDirectory .\accepted-release
.NOTES
    制品目录必须包含 SHA256SUMS.txt。项目版本和制品版本必须一致。
    PYPI_TOKEN 从项目根 .env 读取；发布不修改版本、不重建或删除制品。
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Version,
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ArtifactDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ($Version -notmatch '^\d+\.\d+\.\d+([.a-zA-Z0-9]+)?$') { throw "版本号格式不正确: $Version" }
$artifactRoot = (Resolve-Path -LiteralPath $ArtifactDirectory).Path
$wheels = @(Get-ChildItem -LiteralPath $artifactRoot -File | Where-Object { $_.Name -like "redlotus-$Version-*.whl" })
$sdists = @(Get-ChildItem -LiteralPath $artifactRoot -File | Where-Object { $_.Name -eq "redlotus-$Version.tar.gz" })
if ($wheels.Count -ne 1 -or $sdists.Count -ne 1) { throw '制品目录必须包含指定版本的唯一 wheel 和 sdist' }
$files = @($wheels[0], $sdists[0])
$hashes = @(Get-Content -LiteralPath (Join-Path $artifactRoot 'SHA256SUMS.txt'))
foreach ($file in $files) {
    $pattern = '^([a-fA-F0-9]{64})\s+\*?' + [regex]::Escape($file.Name) + '$'
    $entries = @($hashes | Where-Object { $_ -match $pattern })
    if ($entries.Count -ne 1) { throw "SHA256 清单缺少唯一条目: $($file.Name)" }
    $expectedHash = [regex]::Match($entries[0], $pattern).Groups[1].Value
    if ((Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -ne $expectedHash) {
        throw "制品 SHA256 不匹配: $($file.Name)"
    }
}

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { $python = (Get-Command python -ErrorAction Stop).Source }
$checkMetadata = @'
import email.parser, pathlib, sys, tarfile, tomllib, zipfile
project, version, wheel, sdist = sys.argv[1:]
metadata = tomllib.loads(pathlib.Path(project).read_text(encoding='utf-8'))['project']
if metadata['name'].lower() != 'redlotus' or metadata['version'] != version:
    raise ValueError('Project name/version mismatch')
with zipfile.ZipFile(wheel) as archive:
    names = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
    if len(names) != 1:
        raise ValueError('Wheel must contain exactly one METADATA')
    wheel_metadata = archive.read(names[0]).decode('utf-8')
with tarfile.open(sdist, 'r:gz') as archive:
    names = [entry for entry in archive.getmembers() if len(pathlib.PurePosixPath(entry.name).parts) == 2 and entry.name.endswith('/PKG-INFO') and entry.isfile()]
    if len(names) != 1:
        raise ValueError('Sdist must contain exactly one root PKG-INFO')
    sdist_metadata = archive.extractfile(names[0]).read().decode('utf-8')
for content in (wheel_metadata, sdist_metadata):
    parsed = email.parser.Parser().parsestr(content)
    if parsed['Name'].lower() != 'redlotus' or parsed['Version'] != version:
        raise ValueError('Artifact name/version mismatch')
'@
$checkMetadata | & $python - (Join-Path $root 'pyproject.toml') $Version $files[0].FullName $files[1].FullName
if ($LASTEXITCODE -ne 0) { throw '项目或制品内嵌版本校验失败' }

$published = $false
try {
    Invoke-RestMethod -Uri "https://pypi.org/pypi/RedLotus/$Version/json" | Out-Null
    $published = $true
}
catch {
    $response = $_.Exception.PSObject.Properties['Response']
    if (-not $response -or [int]$response.Value.StatusCode -ne 404) { throw }
}
if ($published) { throw "PyPI 已存在 RedLotus $Version；不重复上传或覆盖" }

$token = $null
foreach ($line in Get-Content -LiteralPath (Join-Path $root '.env')) {
    if ($line -match '^\s*PYPI_TOKEN\s*=\s*(.+?)\s*$') {
        $token = $Matches[1].Trim('"').Trim("'")
        break
    }
}
if ([string]::IsNullOrWhiteSpace($token) -or -not $token.StartsWith('pypi-')) { throw '.env 缺少有效的 PYPI_TOKEN' }
$previousToken = $env:UV_PUBLISH_TOKEN
$uploadPaths = @($files | ForEach-Object { [regex]::Replace($_.FullName, '[*?\[\]]', '[$0]') })
try {
    $env:UV_PUBLISH_TOKEN = $token
    uv publish --no-config --publish-url https://upload.pypi.org/legacy/ --trusted-publishing never @uploadPaths
    if ($LASTEXITCODE -ne 0) { throw 'uv publish 失败；保留当前制品和版本供核查' }
}
finally { $env:UV_PUBLISH_TOKEN = $previousToken }
Write-Host "[OK] 已上传 RedLotus $Version 的已验收 wheel 和 sdist；仍需公开安装验收" -ForegroundColor Green

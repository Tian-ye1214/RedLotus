#Requires -Version 7.0
param([string]$Python)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $Python) { $Python = Join-Path $root '.venv\Scripts\python.exe' }
$Python = (Get-Command $Python).Source
$testRoot = Join-Path $root ('WorkDatabase\runtime\publish-tests-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path (Join-Path $testRoot 'scripts') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'publish.ps1') -Destination (Join-Path $testRoot 'scripts\publish.ps1')
$nativeUv = (Get-Command uv -CommandType Application).Source
$oldLocation = Get-Location
$oldToken = $env:UV_PUBLISH_TOKEN
$oldPath = $env:PATH
$env:PATH = (Split-Path $Python) + [IO.Path]::PathSeparator + $env:PATH
$results = @()
$publishTestState = [pscustomobject]@{ online = $false; exitCode = 0; nativeGlob = $false; uploads = [Collections.Generic.List[object]]::new() }

function Invoke-RestMethod {
    param($Uri)
    if ($Uri -ne 'https://pypi.org/pypi/RedLotus/1.0.1.post1/json') { throw "Unexpected URL: $Uri" }
    if ($publishTestState.online) { return @{ info = @{ version = '1.0.1.post1' } } }
    $response = [Net.Http.HttpResponseMessage]::new([Net.HttpStatusCode]::NotFound)
    throw [Microsoft.PowerShell.Commands.HttpResponseException]::new('Fixture: version not published', $response)
}

function uv {
    $publishTestState.uploads.Add(@($args))
    if ($env:UV_PUBLISH_TOKEN -ne 'pypi-test-only') { throw 'Publish token was not scoped to the subprocess' }
    if ($publishTestState.nativeGlob) {
        $paths = @($args | Where-Object { $_ -match '\.(whl|tar\.gz)$' })
        & $nativeUv publish --dry-run --no-config --trusted-publishing never --publish-url http://127.0.0.1:9/legacy/ @paths
        return
    }
    $global:LASTEXITCODE = $publishTestState.exitCode
}

try {
    foreach ($case in @('accepted', 'project-version', 'wheel-version', 'sdist-version', 'hash', 'online', 'missing-token', 'upload-failure', 'literal-directory')) {
        $publishTestState.uploads.Clear()
        $publishTestState.online = $case -eq 'online'
        $publishTestState.exitCode = if ($case -eq 'upload-failure') { 1 } else { 0 }
        $publishTestState.nativeGlob = $case -eq 'literal-directory'
        $artifacts = Join-Path $testRoot $(if ($publishTestState.nativeGlob) { 'accepted[1]' } else { 'accepted' })
        $env:UV_PUBLISH_TOKEN = 'previous-process-token'
        $fixture = @'
import hashlib, io, pathlib, sys, tarfile, zipfile
root, case = pathlib.Path(sys.argv[1]), sys.argv[2]
version = '1.0.1.post1'
root.joinpath('pyproject.toml').write_text('[project]\nname = "RedLotus"\nversion = "' + ('9.0.0' if case == 'project-version' else version) + '"\n')
root.joinpath('.env').write_text('' if case == 'missing-token' else 'PYPI_TOKEN=pypi-test-only\n')
artifacts = root / ('accepted[1]' if case == 'literal-directory' else 'accepted')
artifacts.mkdir(exist_ok=True)
wheel = artifacts / f'redlotus-{version}-py3-none-any.whl'
with zipfile.ZipFile(wheel, 'w') as archive:
    archive.writestr(f'redlotus-{version}.dist-info/METADATA', 'Metadata-Version: 2.1\nName: RedLotus\nVersion: ' + ('9.0.0' if case == 'wheel-version' else version) + '\n')
sdist = artifacts / f'redlotus-{version}.tar.gz'
with tarfile.open(sdist, 'w:gz') as archive:
    data = ('Metadata-Version: 2.1\nName: RedLotus\nVersion: ' + ('9.0.0' if case == 'sdist-version' else version) + '\n').encode()
    info = tarfile.TarInfo(f'redlotus-{version}/PKG-INFO')
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))
(artifacts / 'SHA256SUMS.txt').write_text(''.join(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n' for path in (wheel, sdist)))
if case == 'hash':
    wheel.write_bytes(wheel.read_bytes() + b'tampered')
(artifacts / 'unrelated.whl').write_bytes(b'must remain untouched')
(root / 'dist').mkdir(exist_ok=True)
(root / 'dist' / 'old.whl').write_bytes(b'must remain untouched')
if case == 'literal-directory':
    shadow = root / 'accepted1'
    shadow.mkdir()
    for path in (wheel, sdist):
        (shadow / path.name).write_bytes(b'unverified sibling artifact; must never be selected')
'@
        $fixture | & $Python - $testRoot $case
        if ($LASTEXITCODE -ne 0) { throw 'Fixture construction failed' }
        $manifestHash = (Get-FileHash -LiteralPath (Join-Path $testRoot 'pyproject.toml')).Hash
        $caught = $null
        try { & (Join-Path $testRoot 'scripts\publish.ps1') -Version '1.0.1.post1' -ArtifactDirectory $artifacts }
        catch { $caught = $_ }
        $shouldUpload = $case -in @('accepted', 'upload-failure', 'literal-directory')
        if (($null -eq $caught) -ne ($case -in @('accepted', 'literal-directory'))) { throw "Unexpected result for ${case}: $caught" }
        if ($publishTestState.uploads.Count -ne [int]$shouldUpload) { throw "Unexpected upload count for $case" }
        if ($shouldUpload) {
            $uploaded = @($publishTestState.uploads[0] | Where-Object { $_ -match '\.(whl|tar\.gz)$' })
            $expected = @((Join-Path $artifacts 'redlotus-1.0.1.post1-py3-none-any.whl'), (Join-Path $artifacts 'redlotus-1.0.1.post1.tar.gz'))
            if ($publishTestState.nativeGlob) { $expected = @($expected | ForEach-Object { $_.Replace('[', '[[]').Replace('1]', '1[]]') }) }
            if (@(Compare-Object $expected $uploaded).Count) { throw "Unexpected artifact selection for $case" }
            if ('build' -in $publishTestState.uploads[0] -or '--token' -in $publishTestState.uploads[0]) { throw 'Build or credential argument reached uv' }
        }
        if ($env:UV_PUBLISH_TOKEN -ne 'previous-process-token') { throw "Token environment was not restored for $case" }
        if ((Get-FileHash -LiteralPath (Join-Path $testRoot 'pyproject.toml')).Hash -ne $manifestHash) { throw 'Project version was mutated' }
        foreach ($sentinel in @((Join-Path $testRoot 'dist\old.whl'), (Join-Path $artifacts 'unrelated.whl'))) {
            if ([IO.File]::ReadAllText($sentinel) -ne 'must remain untouched') { throw "Unrelated artifact was modified: $sentinel" }
        }
        $results += [pscustomobject]@{ case = $case; passed = $true }
    }
    if ($IsWindows) {
        $publisher = (Join-Path $PSScriptRoot 'publish.ps1').Replace("'", "''")
        $legacyCheck = '$tokens=$null; $errors=$null; [System.Management.Automation.Language.Parser]::ParseFile(''' + $publisher + ''',[ref]$tokens,[ref]$errors) | Out-Null; if($errors.Count){$errors | Out-String | Write-Output; exit 1}'
        & (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -NoProfile -NonInteractive -EncodedCommand ([Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($legacyCheck)))
        if ($LASTEXITCODE -ne 0) { throw 'Windows PowerShell 5.1 cannot parse publish.ps1' }
        $results += [pscustomobject]@{ case = 'powershell-5.1-parser'; passed = $true }
    }
    $results | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $testRoot 'results.json') -Encoding utf8
    Write-Host "Publish regression: $($results.Count) passed; evidence: $testRoot"
}
finally {
    $env:UV_PUBLISH_TOKEN = $oldToken
    $env:PATH = $oldPath
    Set-Location $oldLocation
}

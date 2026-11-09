param(
    [string]$InnoCompiler,
    [string]$Python,
    [string]$ServiceUrl = 'https://127.0.0.1:8443',
    [string]$CaCertificate
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).ProviderPath
if (-not $Python) { $Python = Join-Path $repo '.venv\Scripts\python.exe' }
if (-not $InnoCompiler) {
    $installed = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($installed) { $InnoCompiler = $installed.Source }
    else {
        $candidates = @(
            (Join-Path $repo 'build\desktop-tools\inno\ISCC.exe'),
            (Join-Path $env:ProgramFiles 'Inno Setup 7\ISCC.exe'),
            (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe')
        )
        $InnoCompiler = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
            Select-Object -First 1
    }
}
if (-not $InnoCompiler -or -not (Test-Path -LiteralPath $InnoCompiler -PathType Leaf)) {
    throw 'Provide the Inno Setup compiler using -InnoCompiler.'
}
$stage = Join-Path $repo ('build\desktop\' + [guid]::NewGuid().ToString('N'))
$package = Join-Path $stage 'package'
$installers = Join-Path $stage 'installer'
$output = Join-Path $repo 'dist\desktop'
[void][IO.Directory]::CreateDirectory($stage)
[void][IO.Directory]::CreateDirectory($installers)
[void][IO.Directory]::CreateDirectory($output)
$entry = Join-Path $repo 'src\teyvatnotes\desktop\__main__.py'
$data = Join-Path $repo 'src\teyvatnotes\knowledge\data'
$installer = Join-Path $PSScriptRoot 'desktop_installer.iss'
$clientConfig = Join-Path $stage 'client.toml'

Push-Location -LiteralPath $repo
try {
    $appVersion = & $Python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])'
    if ($LASTEXITCODE) { throw 'Unable to resolve the application version.' }
    $origin = & $Python -c 'import sys; from teyvatnotes.desktop.config import normalize_service_url; print(normalize_service_url(sys.argv[1]))' $ServiceUrl
    if ($LASTEXITCODE) { throw 'Invalid HTTPS service URL.' }
    [IO.File]::WriteAllText($clientConfig, "service_url = " + ($origin | ConvertTo-Json -Compress) + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    $defines = @("/DClientConfigFile=$clientConfig")
    if ($CaCertificate) {
        $resolvedCa = (Resolve-Path -LiteralPath $CaCertificate).ProviderPath
        & $Python -c 'from pathlib import Path; import sys; from teyvatnotes.desktop.config import validate_public_ca; validate_public_ca(Path(sys.argv[1]))' $resolvedCa
        if ($LASTEXITCODE) { throw 'The optional CA must contain public certificates only.' }
        $defines += "/DClientCaFile=$resolvedCa"
    }
    $savedPath = $env:Path
    try {
        # Qt 依赖 Windows 的 ICU。开发机 PATH 上的第三方同名 DLL 会遮蔽系统版本。
        $env:Path = (Join-Path $env:SystemRoot 'System32') + ';' + $env:SystemRoot
        & $Python -m PyInstaller --noconfirm --onedir --windowed --name TeyvatNotes `
            --specpath $stage --distpath $package --workpath (Join-Path $stage 'work') `
            --paths (Join-Path $repo 'src') --add-data "${data}:teyvatnotes/knowledge/data" `
            --collect-data certifi `
            --copy-metadata PySide6-Essentials --copy-metadata shiboken6 $entry
        if ($LASTEXITCODE) { throw 'PyInstaller failed.' }
    }
    finally {
        $env:Path = $savedPath
    }
    $bundle = Join-Path $package 'TeyvatNotes'
    if (Test-Path -LiteralPath (Join-Path $bundle '_internal\icuuc.dll')) {
        throw 'A bundled ICU DLL would hide the Windows ICU runtime.'
    }
    & $InnoCompiler "/DAppVersion=$appVersion" "/DBundleDir=$bundle" @defines "/O$installers" $installer
    if ($LASTEXITCODE) { throw 'Inno Setup failed.' }
    $name = 'TeyvatNotes-Setup.exe'
    Copy-Item -LiteralPath (Join-Path $installers $name) -Destination (Join-Path $output $name) -Force
    Write-Output (Join-Path $output $name)
}
finally {
    Pop-Location
}

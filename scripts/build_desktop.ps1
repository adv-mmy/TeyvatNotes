param(
    [string]$InnoCompiler,
    [string]$Python
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
$library = Join-Path $stage 'desktop_guides.sqlite3'
$installer = Join-Path $PSScriptRoot 'desktop_installer.iss'

Push-Location -LiteralPath $repo
try {
    $appVersion = & $Python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])'
    if ($LASTEXITCODE) { throw 'Unable to resolve the application version.' }
    & $Python -m teyvatnotes.desktop.library $library
    if ($LASTEXITCODE) { throw 'Unable to build the starter guide database.' }
    $savedPath = $env:Path
    try {
        # Qt 依赖 Windows 的 ICU。开发机 PATH 上的第三方同名 DLL 会遮蔽系统版本。
        $env:Path = (Join-Path $env:SystemRoot 'System32') + ';' + $env:SystemRoot
        & $Python -m PyInstaller --noconfirm --onedir --windowed --name TeyvatNotes `
            --specpath $stage --distpath $package --workpath (Join-Path $stage 'work') `
            --paths (Join-Path $repo 'src') --add-data "${data}:teyvatnotes/knowledge/data" `
            --add-data "${library}:teyvatnotes/knowledge/data" --collect-data certifi `
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
    & $InnoCompiler "/DAppVersion=$appVersion" "/DBundleDir=$bundle" "/O$installers" $installer
    if ($LASTEXITCODE) { throw 'Inno Setup failed.' }
    $name = 'TeyvatNotes-Setup.exe'
    Copy-Item -LiteralPath (Join-Path $installers $name) -Destination (Join-Path $output $name) -Force
    Write-Output (Join-Path $output $name)
}
finally {
    Pop-Location
}

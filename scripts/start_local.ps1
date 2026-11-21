param(
    [string]$EnvFile,
    [string]$TlsDirectory = (Join-Path $PSScriptRoot '..\build\local-tls'),
    [ValidateRange(1, 65535)][int]$Port = 8443
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).ProviderPath
$certificate = (Resolve-Path -LiteralPath (Join-Path $TlsDirectory 'service-ca.pem')).ProviderPath
$privateKey = (Resolve-Path -LiteralPath (Join-Path $TlsDirectory 'service-key.pem')).ProviderPath
$prefix = @('run', '--locked')
if ($EnvFile) {
    $envFilePath = (Resolve-Path -LiteralPath $EnvFile).ProviderPath.Replace('\', '/')
    $prefix += @('--env-file', $envFilePath)
}
$previousMode = [Environment]::GetEnvironmentVariable('GUIDE_DEPLOYMENT_MODE', 'Process')
Push-Location -LiteralPath $root
try {
    $env:GUIDE_DEPLOYMENT_MODE = 'local'
    & uv @prefix uvicorn api_main:app --host 127.0.0.1 --port $Port --workers 1 `
        --ssl-certfile $certificate --ssl-keyfile $privateKey `
        --no-proxy-headers --no-access-log --timeout-graceful-shutdown 10
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
}
finally {
    [Environment]::SetEnvironmentVariable('GUIDE_DEPLOYMENT_MODE', $previousMode, 'Process')
    Pop-Location
}

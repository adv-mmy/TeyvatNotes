param([string]$Directory = (Join-Path $PSScriptRoot '..\build\local-tls'))
$ErrorActionPreference = 'Stop'
$target = [IO.Path]::GetFullPath($Directory)
$certPath = Join-Path $target 'service-ca.pem'
$keyPath = Join-Path $target 'service-key.pem'
if ((Test-Path -LiteralPath $certPath -PathType Leaf) -and (Test-Path -LiteralPath $keyPath -PathType Leaf)) {
    Write-Output "复用本地公开证书：$certPath"
    return
}
if ((Test-Path -LiteralPath $certPath) -or (Test-Path -LiteralPath $keyPath)) {
    throw '证书或私钥已存在，拒绝覆盖。'
}
[void][IO.Directory]::CreateDirectory($target)
$rsa = [Security.Cryptography.RSA]::Create(3072)
try {
    $request = [Security.Cryptography.X509Certificates.CertificateRequest]::new(
        'CN=TeyvatNotes local service', $rsa,
        [Security.Cryptography.HashAlgorithmName]::SHA256,
        [Security.Cryptography.RSASignaturePadding]::Pkcs1
    )
    $san = [Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder]::new()
    $san.AddIpAddress([Net.IPAddress]::Loopback)
    $san.AddDnsName('localhost')
    $request.CertificateExtensions.Add($san.Build())
    $request.CertificateExtensions.Add(
        [Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($true, $true, 0, $true)
    )
    $request.CertificateExtensions.Add(
        [Security.Cryptography.X509Certificates.X509SubjectKeyIdentifierExtension]::new($request.PublicKey, $false)
    )
    $now = [DateTimeOffset]::UtcNow
    $certificate = $request.CreateSelfSigned($now.AddMinutes(-5), $now.AddYears(1))
    try {
        foreach ($file in @(
            @{path=$keyPath; contents=$rsa.ExportPkcs8PrivateKeyPem()},
            @{path=$certPath; contents=$certificate.ExportCertificatePem()}
        )) {
            $stream = [IO.File]::Open($file.path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
            try {
                $bytes = [Text.Encoding]::ASCII.GetBytes($file.contents + "`n")
                $stream.Write($bytes, 0, $bytes.Length)
            }
            finally { $stream.Dispose() }
        }
    }
    finally { $certificate.Dispose() }
}
finally { $rsa.Dispose() }
Write-Output "本地公开证书：$certPath"
Write-Output "服务端私钥保存在独立文件中，勿打包或分享。"

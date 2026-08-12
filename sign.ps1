[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9A-Fa-f ]{40,59}$')]
    [string] $CertificateThumbprint,

    [Parameter(Mandatory = $true)]
    [uri] $TimestampServer,

    [string] $Executable = (Join-Path $PSScriptRoot 'dist\QuickAccess.exe'),

    [string] $SignToolPath
)

$ErrorActionPreference = 'Stop'
$codeSigningOid = '1.3.6.1.5.5.7.3.3'
$thumbprint = ($CertificateThumbprint -replace '\s', '').ToUpperInvariant()
if ($thumbprint -notmatch '^[0-9A-F]{40}$') {
    throw 'Certificate thumbprint must contain exactly 40 hexadecimal characters.'
}
$executablePath = (Resolve-Path -LiteralPath $Executable).Path

if ([IO.Path]::GetExtension($executablePath) -ne '.exe') {
    throw "Authenticode target must be an EXE: $executablePath"
}

$certificate = $null
$certificateStore = $null
foreach ($store in @('CurrentUser', 'LocalMachine')) {
    $candidate = Get-Item -LiteralPath "Cert:\$store\My\$thumbprint" -ErrorAction SilentlyContinue
    if ($null -ne $candidate) {
        $certificate = $candidate
        $certificateStore = $store
        break
    }
}

if ($null -eq $certificate) {
    throw "Certificate was not found in CurrentUser\My or LocalMachine\My: $thumbprint"
}
if (-not $certificate.HasPrivateKey) {
    throw 'The selected certificate does not have an accessible private key.'
}
if ($certificate.NotBefore -gt (Get-Date) -or $certificate.NotAfter -le (Get-Date)) {
    throw 'The selected certificate is not currently valid.'
}
$ekuOids = @($certificate.EnhancedKeyUsageList | ForEach-Object { $_.ObjectId.Value })
if ($codeSigningOid -notin $ekuOids) {
    throw 'The selected certificate is not valid for Code Signing.'
}

if (-not $SignToolPath) {
    $sdkRoot = "${env:ProgramFiles(x86)}\Windows Kits\10\bin"
    if (Test-Path -LiteralPath $sdkRoot) {
        $SignToolPath = Get-ChildItem -LiteralPath $sdkRoot -Recurse -Filter signtool.exe |
            Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
            Sort-Object FullName -Descending |
            Select-Object -First 1 -ExpandProperty FullName
    }
}
if (-not $SignToolPath -or -not (Test-Path -LiteralPath $SignToolPath)) {
    throw 'signtool.exe was not found. Install Windows SDK Signing Tools or pass -SignToolPath.'
}

$signArguments = @(
    'sign',
    '/sha1', $thumbprint,
    '/s', 'My',
    '/fd', 'SHA256',
    '/tr', $TimestampServer.AbsoluteUri,
    '/td', 'SHA256',
    '/v'
)
if ($certificateStore -eq 'LocalMachine') {
    $signArguments += '/sm'
}
$signArguments += $executablePath

& $SignToolPath @signArguments
if ($LASTEXITCODE -ne 0) {
    throw "signtool failed with exit code $LASTEXITCODE"
}

$signature = Get-AuthenticodeSignature -LiteralPath $executablePath
if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate) {
    throw "Signature verification failed: $($signature.Status) $($signature.StatusMessage)"
}
if ($signature.SignerCertificate.Thumbprint.ToUpperInvariant() -ne $thumbprint) {
    throw 'The resulting signature does not match the requested certificate.'
}
if ($null -eq $signature.TimeStamperCertificate) {
    throw 'The EXE was signed but no trusted timestamp was attached.'
}

$hash = Get-FileHash -LiteralPath $executablePath -Algorithm SHA256
[pscustomobject]@{
    Executable = $executablePath
    Subject = $signature.SignerCertificate.Subject
    Thumbprint = $signature.SignerCertificate.Thumbprint
    TimestampSubject = $signature.TimeStamperCertificate.Subject
    Status = $signature.Status
    SHA256 = $hash.Hash
}

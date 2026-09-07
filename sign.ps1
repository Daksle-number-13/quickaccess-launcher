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
$manifestPath = Join-Path (Split-Path -Parent $executablePath) 'QuickAccess.release.json'
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "Release manifest is missing. Build with build.ps1 before signing: $manifestPath"
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$expectedLicenseName = "QuickAccess_Third_Party_Licenses_v$($manifest.version).zip"
if (
    $manifest.schema_version -ne 2 -or
    $manifest.product -ne 'QuickAccess Launcher' -or
    $manifest.version -notmatch '^\d+\.\d+\.\d+$' -or
    $manifest.build.release_eligible -ne $true -or
    $manifest.build.source_dirty -ne $false -or
    $manifest.build.source_commit -notmatch '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'
) {
    throw 'Release manifest is not a clean, tagged, schema-v2 signing candidate.'
}
$unsignedHash = Get-FileHash -LiteralPath $executablePath -Algorithm SHA256
if (
    $manifest.artifact.name -ne [IO.Path]::GetFileName($executablePath) -or
    $manifest.artifact.sha256 -notmatch '^[0-9A-F]{64}$' -or
    $manifest.artifact.sha256 -ne $unsignedHash.Hash -or
    $manifest.authenticode.signed -ne $false -or
    $manifest.authenticode.status -ne 'NotSigned'
) {
    throw 'Release manifest does not match the executable being signed.'
}
$companionArtifacts = @($manifest.companion_artifacts)
if ($companionArtifacts.Count -ne 1) {
    throw 'Release manifest does not contain the required license companion artifact.'
}
foreach ($companion in $companionArtifacts) {
    $companionName = [IO.Path]::GetFileName([string]$companion.name)
    if (
        $companionName -cne [string]$companion.name -or
        $companionName -cne $expectedLicenseName -or
        [string]$companion.sha256 -notmatch '^[0-9A-F]{64}$'
    ) {
        throw "Unsafe companion artifact name in release manifest: $($companion.name)"
    }
    $companionPath = Join-Path (Split-Path -Parent $executablePath) $companionName
    if (-not (Test-Path -LiteralPath $companionPath -PathType Leaf)) {
        throw "Release companion artifact is missing: $companionPath"
    }
    $companionHash = Get-FileHash -LiteralPath $companionPath -Algorithm SHA256
    if ($companionHash.Hash -ne [string]$companion.sha256) {
        throw "Release companion artifact hash mismatch: $companionName"
    }
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
$manifest.artifact.size = (Get-Item -LiteralPath $executablePath).Length
$manifest.artifact.sha256 = $hash.Hash
$manifest.authenticode.status = $signature.Status.ToString()
$manifest.authenticode.signed = $true
$manifestJson = $manifest | ConvertTo-Json -Depth 8
$manifestTemp = "$manifestPath.tmp"
[IO.File]::WriteAllText(
    $manifestTemp,
    $manifestJson + [Environment]::NewLine,
    [Text.UTF8Encoding]::new($false)
)
Move-Item -LiteralPath $manifestTemp -Destination $manifestPath -Force

[pscustomobject]@{
    Executable = $executablePath
    Manifest = $manifestPath
    Subject = $signature.SignerCertificate.Subject
    Thumbprint = $signature.SignerCertificate.Thumbprint
    TimestampSubject = $signature.TimeStamperCertificate.Subject
    Status = $signature.Status
    SHA256 = $hash.Hash
}

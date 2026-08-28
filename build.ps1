param(
    [switch]$Clean,
    [string]$PythonExecutable = "python",
    [Nullable[long]]$SourceDateEpoch,
    [switch]$SmokeTest
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if ($Clean) {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue `
        -LiteralPath (Join-Path $PSScriptRoot "build"), (Join-Path $PSScriptRoot "dist")
}

$pythonProbe = @(& $PythonExecutable -c "import struct, sys; print('.'.join(map(str, sys.version_info[:3]))); print(sys.platform); print(struct.calcsize('P') * 8)")
if ($LASTEXITCODE -ne 0) {
    throw "Could not run the selected Python interpreter: $PythonExecutable"
}
if ($pythonProbe.Count -lt 3) {
    throw "Could not identify the selected Python interpreter: $PythonExecutable"
}
$parsedVersion = [version]$pythonProbe[0].Trim()
$pythonPlatform = $pythonProbe[1].Trim()
$pythonBits = [int]$pythonProbe[2].Trim()
$releaseVersion = [version]"3.13.15"
$releasePyInstallerVersion = [version]"6.19.0"
if ($parsedVersion -ne $releaseVersion -or $pythonPlatform -ne "win32" -or $pythonBits -ne 64) {
    throw "Release builds require Windows x64 Python $releaseVersion exactly; selected: $parsedVersion $pythonPlatform $pythonBits-bit"
}

$projectVersion = (& $PythonExecutable devtools\check_release_metadata.py --format plain).Trim()
if ($LASTEXITCODE -ne 0 -or -not $projectVersion) {
    throw "Release metadata validation failed."
}

& $PythonExecutable -m pip check
if ($LASTEXITCODE -ne 0) {
    throw "The selected Python environment has inconsistent dependencies."
}

$pyInstallerVersion = (& $PythonExecutable -c "import PyInstaller; print(PyInstaller.__version__)").Trim()
if ($LASTEXITCODE -ne 0 -or [version]$pyInstallerVersion -ne $releasePyInstallerVersion) {
    throw "Release builds require PyInstaller $releasePyInstallerVersion exactly; selected: $pyInstallerVersion"
}

if ($null -eq $SourceDateEpoch) {
    $gitEpoch = (& git -C $PSScriptRoot log -1 --format=%ct 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or $gitEpoch -notmatch '^\d+$') {
        throw "Could not derive SOURCE_DATE_EPOCH from Git. Pass -SourceDateEpoch explicitly."
    }
    $SourceDateEpoch = [long]$gitEpoch
}
if ($SourceDateEpoch -lt 0) {
    throw "SourceDateEpoch must not be negative."
}

# Fixed hash iteration and source timestamps remove avoidable build variance.
# Identical output still requires identical source, interpreter, dependencies,
# Windows SDK/runtime, and PyInstaller bootloader.
$env:PYTHONHASHSEED = "0"
$env:SOURCE_DATE_EPOCH = $SourceDateEpoch.ToString([Globalization.CultureInfo]::InvariantCulture)

& $PythonExecutable -m PyInstaller --noconfirm --clean quickaccess.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$artifact = Join-Path $PSScriptRoot "dist\QuickAccess.exe"
if (-not (Test-Path -LiteralPath $artifact -PathType Leaf)) {
    throw "PyInstaller reported success but the executable is missing: $artifact"
}

$signature = Get-AuthenticodeSignature -LiteralPath $artifact
$signatureStatus = $signature.Status.ToString()
$manifest = Join-Path $PSScriptRoot "dist\QuickAccess.release.json"
& $PythonExecutable devtools\release_manifest.py `
    --artifact $artifact `
    --output $manifest `
    --version $projectVersion `
    --signature-status $signatureStatus `
    --python-version $parsedVersion.ToString() `
    --pyinstaller-version $pyInstallerVersion `
    --source-date-epoch $SourceDateEpoch
if ($LASTEXITCODE -ne 0) {
    throw "Release manifest generation failed."
}

if ($SmokeTest) {
    & $artifact --smoke-test
    if ($LASTEXITCODE -ne 0) {
        throw "Release smoke test failed with exit code $LASTEXITCODE"
    }
}

Write-Host "Built: $artifact"
Write-Host "Manifest: $manifest"
if ($signatureStatus -ne "Valid") {
    Write-Warning "Authenticode status is $signatureStatus. Sign before release or publish this unsigned status and SHA-256 honestly."
}

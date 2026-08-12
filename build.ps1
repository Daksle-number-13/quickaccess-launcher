param(
    [switch]$Clean,
    [string]$PythonExecutable = "python"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if ($Clean) {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue `
        -LiteralPath (Join-Path $PSScriptRoot "build"), (Join-Path $PSScriptRoot "dist")
}

$pythonVersion = & $PythonExecutable -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
if ($LASTEXITCODE -ne 0) {
    throw "Could not run the selected Python interpreter: $PythonExecutable"
}
$parsedVersion = [version]$pythonVersion.Trim()
$minimumReleaseVersion = [version]"3.13.15"
if ($parsedVersion -lt $minimumReleaseVersion) {
    throw "Release builds require Python $minimumReleaseVersion or newer; selected: $parsedVersion"
}

& $PythonExecutable -m PyInstaller --noconfirm --clean quickaccess.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$artifact = Join-Path $PSScriptRoot "dist\QuickAccess.exe"
if (-not (Test-Path -LiteralPath $artifact -PathType Leaf)) {
    throw "PyInstaller reported success but the executable is missing: $artifact"
}
Write-Host "Built: $artifact"

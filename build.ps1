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
if ($parsedVersion -ne $releaseVersion -or $pythonPlatform -ne "win32" -or $pythonBits -ne 64) {
    throw "Release builds require Windows x64 Python $releaseVersion exactly; selected: $parsedVersion $pythonPlatform $pythonBits-bit"
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

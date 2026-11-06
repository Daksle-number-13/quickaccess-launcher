param(
    [switch]$Clean,
    [switch]$SmokeTest,
    [ValidateRange(5, 120)]
    [int]$SmokeTimeoutSeconds = 30,
    [switch]$AllowDirty,
    [switch]$SkipTests,
    [string]$PythonExecutable = "python",
    [string]$CargoExecutable
)

# Release build of the Rust port (v3 and later). Produces the same three
# files as build.ps1, so sign.ps1 and the release steps do not change.

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Invoke-Checked {
    param([string]$Description, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

if (-not $CargoExecutable) {
    $found = Get-Command cargo -ErrorAction SilentlyContinue
    $CargoExecutable = if ($found) {
        $found.Source
    } else {
        Join-Path $env:USERPROFILE ".cargo\bin\cargo.exe"
    }
}
if (-not (Test-Path -LiteralPath $CargoExecutable -PathType Leaf)) {
    throw "cargo was not found. Install Rust with the MSVC toolchain or pass -CargoExecutable."
}
$rustcExecutable = Join-Path (Split-Path -Parent $CargoExecutable) "rustc.exe"
if (-not (Test-Path -LiteralPath $rustcExecutable -PathType Leaf)) {
    $rustcExecutable = "rustc"
}
$rustVersionLine = (& $rustcExecutable --version).Trim()
if ($LASTEXITCODE -ne 0 -or $rustVersionLine -notmatch '^rustc (\d+\.\d+\.\d+)') {
    throw "Could not identify rustc: $rustVersionLine"
}
$rustVersion = $Matches[1]

# --- Version consistency -----------------------------------------------------
$crateRoot = Join-Path $PSScriptRoot "rust"
$cargoToml = Get-Content -LiteralPath (Join-Path $crateRoot "Cargo.toml") -Raw -Encoding UTF8
if ($cargoToml -notmatch '(?ms)^\[package\].*?^version\s*=\s*"(\d+\.\d+\.\d+)"') {
    throw "rust/Cargo.toml has no numeric MAJOR.MINOR.PATCH package version."
}
$projectVersion = $Matches[1]
$versionCommas = ($projectVersion -replace '\.', ',') + ",0"
$fileVersion = "$projectVersion.0"
$resource = Get-Content -LiteralPath (Join-Path $crateRoot "resources\quickaccess.rc") -Raw -Encoding UTF8
foreach ($expected in @(
    "FILEVERSION $versionCommas",
    "PRODUCTVERSION $versionCommas",
    "`"FileVersion`", `"$fileVersion`"",
    "`"ProductVersion`", `"$fileVersion`""
)) {
    if (-not $resource.Contains($expected)) {
        throw "rust/resources/quickaccess.rc does not contain: $expected"
    }
}
$cargoLock = Get-Content -LiteralPath (Join-Path $crateRoot "Cargo.lock") -Raw -Encoding UTF8
if ($cargoLock -notmatch "(?m)^name = `"quickaccess`"\r?\nversion = `"$([regex]::Escape($projectVersion))`"") {
    throw "rust/Cargo.lock does not record quickaccess $projectVersion. Run cargo build once."
}

# --- Source provenance -------------------------------------------------------
$gitCommit = (& git -C $PSScriptRoot rev-parse HEAD 2>$null).Trim()
if ($LASTEXITCODE -ne 0 -or $gitCommit -notmatch '^[0-9a-fA-F]{40,64}$') {
    throw "Could not resolve the source Git commit."
}
$gitStatus = @(& git -C $PSScriptRoot status --porcelain --untracked-files=normal)
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect the source worktree."
}
$sourceDirty = $gitStatus.Count -gt 0
$expectedTag = "v$projectVersion"
$tagsAtHead = @(& git -C $PSScriptRoot tag --points-at HEAD)
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect Git tags for the source commit."
}
$hasExpectedTag = $tagsAtHead -ccontains $expectedTag
if (-not $AllowDirty) {
    if (-not $Clean) { throw "Release builds require -Clean so stale artifacts cannot be reused." }
    if (-not $SmokeTest) { throw "Release builds require -SmokeTest." }
    if ($SkipTests) { throw "-SkipTests is allowed only with -AllowDirty verification builds." }
    if ($sourceDirty) {
        throw "Release builds require a clean worktree. Commit all intended changes or pass -AllowDirty for a non-release verification build."
    }
    if (-not $hasExpectedTag) {
        throw "Release builds require HEAD to carry the exact tag $expectedTag. Pass -AllowDirty only for a non-release verification build."
    }
} else {
    if ($sourceDirty) { Write-Warning "Building from a dirty worktree; the manifest will record source_dirty=true." }
    if (-not $hasExpectedTag) { Write-Warning "HEAD is not tagged $expectedTag; this is a non-release verification build." }
}
$gitEpoch = (& git -C $PSScriptRoot log -1 --format=%ct 2>$null).Trim()
if ($LASTEXITCODE -ne 0 -or $gitEpoch -notmatch '^\d+$') {
    throw "Could not read the source commit time."
}
$sourceDateEpoch = [long]$gitEpoch

# --- Outputs -----------------------------------------------------------------
$distDirectory = Join-Path $PSScriptRoot "dist"
$artifact = Join-Path $distDirectory "QuickAccess.exe"
$manifest = Join-Path $distDirectory "QuickAccess.release.json"
$licenseBundle = Join-Path $distDirectory "QuickAccess_Third_Party_Licenses_v$projectVersion.zip"
# A separate target directory, so a copy of rust\target\release that is
# running as the user's launcher never blocks or contaminates a release.
$targetDirectory = Join-Path $PSScriptRoot "build\rust-target"

$running = @(Get-Process -Name QuickAccess -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -and [string]::Equals($_.Path, $artifact, [StringComparison]::OrdinalIgnoreCase)
})
if ($running.Count -gt 0) {
    throw "dist\QuickAccess.exe is running (PID $($running[0].Id)). Quit it from the tray before building."
}
if ($Clean) {
    Remove-Item -LiteralPath $targetDirectory, $distDirectory -Recurse -Force -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath $artifact, $manifest, $licenseBundle -Force -ErrorAction SilentlyContinue

# --- Compile -----------------------------------------------------------------
# Static CRT: no Visual C++ Redistributable needed. Path remapping keeps the
# builder's user name and folders out of panic messages, and PDBALTPATH keeps
# only the PDB file name in the debug directory. The encoded form survives the
# spaces in this repository's path.
$cargoHome = if ($env:CARGO_HOME) { $env:CARGO_HOME } else { Join-Path $env:USERPROFILE ".cargo" }
$rustFlags = @(
    "-Ctarget-feature=+crt-static",
    "--remap-path-prefix=$([IO.Path]::GetFullPath($cargoHome))=/cargo",
    "--remap-path-prefix=$([IO.Path]::GetFullPath($PSScriptRoot))=/src",
    "-Clink-arg=/PDBALTPATH:%_PDB%"
)
$previousEncodedFlags = $env:CARGO_ENCODED_RUSTFLAGS
$previousTargetDirectory = $env:CARGO_TARGET_DIR
$env:CARGO_ENCODED_RUSTFLAGS = $rustFlags -join [char]0x1f
$env:CARGO_TARGET_DIR = $targetDirectory
$cargoManifest = Join-Path $crateRoot "Cargo.toml"
try {
    if (-not $SkipTests) {
        Invoke-Checked "cargo test" { & $CargoExecutable test --locked --manifest-path $cargoManifest }
    }
    Invoke-Checked "cargo build" { & $CargoExecutable build --release --locked --manifest-path $cargoManifest }
} finally {
    $env:CARGO_ENCODED_RUSTFLAGS = $previousEncodedFlags
    $env:CARGO_TARGET_DIR = $previousTargetDirectory
}

New-Item -ItemType Directory -Force -Path $distDirectory | Out-Null
Copy-Item -LiteralPath (Join-Path $targetDirectory "release\QuickAccess.exe") -Destination $artifact -Force

# --- Artifact checks ---------------------------------------------------------
$actualFileVersion = (Get-Item -LiteralPath $artifact).VersionInfo.FileVersion
if ($actualFileVersion -ne $fileVersion) {
    Remove-Item -LiteralPath $artifact -Force
    throw "Built EXE version is $actualFileVersion, expected $fileVersion"
}
$artifactText = [Text.Encoding]::GetEncoding(28591).GetString([IO.File]::ReadAllBytes($artifact))
$leaks = @($env:USERPROFILE, $PSScriptRoot, $cargoHome) | Where-Object {
    $_ -and $artifactText.IndexOf($_, [StringComparison]::OrdinalIgnoreCase) -ge 0
}
if ($leaks.Count -gt 0) {
    Remove-Item -LiteralPath $artifact -Force
    throw "The EXE embeds a local build path: $($leaks[0])"
}
if ($artifactText.IndexOf("VCRUNTIME140", [StringComparison]::OrdinalIgnoreCase) -ge 0) {
    Remove-Item -LiteralPath $artifact -Force
    throw "The EXE depends on the Visual C++ Redistributable; the static CRT was not applied."
}

if ($SmokeTest) {
    $smokeTempRoot = Join-Path $PSScriptRoot ("build\smoke-temp-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $smokeTempRoot | Out-Null
    $originalTemp = $env:TEMP
    $originalTmp = $env:TMP
    $smokeProcess = $null
    $smokeFailure = $null
    try {
        $env:TEMP = $smokeTempRoot
        $env:TMP = $smokeTempRoot
        $smokeProcess = Start-Process -FilePath $artifact -ArgumentList "--smoke-test" -PassThru
        if (-not $smokeProcess.WaitForExit($SmokeTimeoutSeconds * 1000)) {
            Stop-Process -Id $smokeProcess.Id -Force -ErrorAction SilentlyContinue
            throw "Release smoke test timed out after $SmokeTimeoutSeconds seconds."
        }
        if ($smokeProcess.ExitCode -ne 0) {
            throw "Release smoke test failed with exit code $($smokeProcess.ExitCode)"
        }
    } catch {
        $smokeFailure = $_
    } finally {
        $env:TEMP = $originalTemp
        $env:TMP = $originalTmp
        if ($null -ne $smokeProcess -and -not $smokeProcess.HasExited) {
            Stop-Process -Id $smokeProcess.Id -Force -ErrorAction SilentlyContinue
            $null = $smokeProcess.WaitForExit(5000)
        }
        Remove-Item -LiteralPath $smokeTempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($null -ne $smokeFailure) {
        Remove-Item -LiteralPath $artifact -Force -ErrorAction SilentlyContinue
        throw $smokeFailure
    }
}

# --- Re-check provenance after the build ------------------------------------
$finalGitCommit = (& git -C $PSScriptRoot rev-parse HEAD 2>$null).Trim()
$finalGitStatus = @(& git -C $PSScriptRoot status --porcelain --untracked-files=normal)
$finalTagsAtHead = @(& git -C $PSScriptRoot tag --points-at HEAD)
$finalSourceDirty = $finalGitStatus.Count -gt 0
if (-not $AllowDirty) {
    if ($finalGitCommit -cne $gitCommit -or $finalSourceDirty -or -not ($finalTagsAtHead -ccontains $expectedTag)) {
        throw "Source commit, worktree, or release tag changed during the build. Discarding release eligibility."
    }
} else {
    $gitCommit = $finalGitCommit
    $sourceDirty = $finalSourceDirty
}

# --- Companion files ---------------------------------------------------------
$signatureStatus = (Get-AuthenticodeSignature -LiteralPath $artifact).Status.ToString()
Invoke-Checked "License bundle generation" {
    & $PythonExecutable devtools\rust_release.py licenses `
        --output $licenseBundle `
        --version $projectVersion `
        --source-date-epoch $sourceDateEpoch `
        --cargo $CargoExecutable `
        --manifest-path $cargoManifest
}
$sourceDirtyText = if ($sourceDirty) { "true" } else { "false" }
$releaseEligibleText = if ($AllowDirty) { "false" } else { "true" }
Invoke-Checked "Release manifest generation" {
    & $PythonExecutable devtools\rust_release.py manifest `
        --artifact $artifact `
        --output $manifest `
        --version $projectVersion `
        --signature-status $signatureStatus `
        --rust-version $rustVersion `
        --source-date-epoch $sourceDateEpoch `
        --source-commit $gitCommit `
        --source-dirty $sourceDirtyText `
        --release-eligible $releaseEligibleText `
        --license-bundle $licenseBundle
}

Write-Host "Built: $artifact"
Write-Host "Manifest: $manifest"
Write-Host "Licenses: $licenseBundle"
if ($signatureStatus -ne "Valid") {
    Write-Warning "Authenticode status is $signatureStatus. Sign before release or publish this unsigned status and SHA-256 honestly."
}

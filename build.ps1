param(
    [switch]$Clean,
    [string]$PythonExecutable = "python",
    [Nullable[long]]$SourceDateEpoch,
    [switch]$SmokeTest,
    [ValidateRange(5, 120)]
    [int]$SmokeTimeoutSeconds = 30,
    [switch]$AllowDirty,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Stop-QuickAccessProcessTree {
    param(
        [Parameter(Mandatory = $true)]
        [System.Diagnostics.Process]$Process
    )

    try {
        $Process.Refresh()
        if ($Process.HasExited) {
            return
        }
    } catch {
        return
    }

    try {
        # PowerShell 7/.NET exposes the process-tree overload directly.
        $Process.Kill($true)
    } catch {
        # Windows PowerShell 5.1 lacks Kill(Boolean).  taskkill's /T option is
        # the native bounded fallback for PyInstaller's parent/child tree and
        # does not depend on WMI/CIM permissions.  Always attempt the known
        # root as well if taskkill is unavailable or rejects the request.
        $systemRoot = [Environment]::GetEnvironmentVariable("SystemRoot")
        $taskkillPath = if ($systemRoot) {
            Join-Path $systemRoot "System32\taskkill.exe"
        } else {
            $null
        }
        $treeKillSucceeded = $false
        if ($taskkillPath -and (Test-Path -LiteralPath $taskkillPath -PathType Leaf)) {
            & $taskkillPath /PID $Process.Id /T /F 2>$null | Out-Null
            $treeKillSucceeded = $LASTEXITCODE -eq 0
        }
        if (-not $treeKillSucceeded) {
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        }
    }

    if (-not $Process.WaitForExit(5000)) {
        throw "Timed-out smoke-test process tree could not be stopped within 5 seconds."
    }
}

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
$artifact = Join-Path $PSScriptRoot "dist\QuickAccess.exe"
$manifest = Join-Path $PSScriptRoot "dist\QuickAccess.release.json"
$licenseBundle = Join-Path $PSScriptRoot "dist\QuickAccess_Third_Party_Licenses_v$projectVersion.zip"
# Never let a failed non-clean verification build leave old companion files
# beside a newly attempted executable.
Remove-Item -LiteralPath $manifest, $licenseBundle -Force -ErrorAction SilentlyContinue

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
    if (-not $Clean) {
        throw "Release builds require -Clean so stale artifacts cannot be reused."
    }
    if (-not $SmokeTest) {
        throw "Release builds require -SmokeTest."
    }
    if ($sourceDirty) {
        throw "Release builds require a clean worktree. Commit all intended changes or pass -AllowDirty for a non-release verification build."
    }
    if (-not $hasExpectedTag) {
        throw "Release builds require HEAD to carry the exact tag $expectedTag. Pass -AllowDirty only for a non-release verification build."
    }
} else {
    if ($sourceDirty) {
        Write-Warning "Building from a dirty worktree; the release manifest will record source_dirty=true."
    }
    if (-not $hasExpectedTag) {
        Write-Warning "HEAD is not tagged $expectedTag; this is a non-release verification build."
    }
}
if ($SkipTests -and -not $AllowDirty) {
    throw "-SkipTests is allowed only with -AllowDirty verification builds. Release builds must run the full test and compile gates."
}

& $PythonExecutable -m pip check
if ($LASTEXITCODE -ne 0) {
    throw "The selected Python environment has inconsistent dependencies."
}
& $PythonExecutable devtools\check_locked_environment.py requirements-dev.txt
if ($LASTEXITCODE -ne 0) {
    throw "The selected Python environment does not match the pinned release requirements."
}

if (-not $SkipTests) {
    & $PythonExecutable -m pytest -q -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) {
        throw "Test suite failed."
    }
    & $PythonExecutable -m compileall -q main.py quickaccess tests devtools
    if ($LASTEXITCODE -ne 0) {
        throw "Python byte-compilation failed."
    }
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
    Remove-Item -LiteralPath $artifact -Force -ErrorAction SilentlyContinue
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

if (-not (Test-Path -LiteralPath $artifact -PathType Leaf)) {
    throw "PyInstaller reported success but the executable is missing: $artifact"
}
$expectedFileVersion = "$projectVersion.0"
$actualFileVersion = (Get-Item -LiteralPath $artifact).VersionInfo.FileVersion
if ($actualFileVersion -ne $expectedFileVersion) {
    throw "Built EXE version is $actualFileVersion, expected $expectedFileVersion"
}

if ($SmokeTest) {
    $smokeProcess = $null
    $smokeFailure = $null
    $smokeCleanupFailure = $null
    $smokeTempBase = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "build"))
    $smokeTempRoot = [IO.Path]::GetFullPath(
        (Join-Path $smokeTempBase ("smoke-temp-" + [guid]::NewGuid().ToString("N")))
    )
    $expectedSmokePrefix = $smokeTempBase.TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    ) + [IO.Path]::DirectorySeparatorChar
    if (-not $smokeTempRoot.StartsWith(
        $expectedSmokePrefix,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to use an unexpected smoke-test temporary path: $smokeTempRoot"
    }
    New-Item -ItemType Directory -Force -Path $smokeTempRoot | Out-Null
    $originalTemp = $env:TEMP
    $originalTmp = $env:TMP
    try {
        # Isolate both tempfile.TemporaryDirectory and PyInstaller's _MEI
        # extraction so even a force-stopped smoke process is recoverable.
        $env:TEMP = $smokeTempRoot
        $env:TMP = $smokeTempRoot
        $smokeProcess = Start-Process `
            -FilePath $artifact `
            -ArgumentList "--smoke-test" `
            -WindowStyle Hidden `
            -PassThru
        $smokeExited = $smokeProcess.WaitForExit($SmokeTimeoutSeconds * 1000)
        if (-not $smokeExited) {
            Stop-QuickAccessProcessTree -Process $smokeProcess
            throw "Release smoke test timed out after $SmokeTimeoutSeconds seconds."
        }
        $smokeExitCode = $smokeProcess.ExitCode
        if ($smokeExitCode -ne 0) {
            throw "Release smoke test failed with exit code $smokeExitCode"
        }
    } catch {
        $smokeFailure = $_
    } finally {
        if ($null -eq $originalTemp) {
            Remove-Item Env:TEMP -ErrorAction SilentlyContinue
        } else {
            $env:TEMP = $originalTemp
        }
        if ($null -eq $originalTmp) {
            Remove-Item Env:TMP -ErrorAction SilentlyContinue
        } else {
            $env:TMP = $originalTmp
        }
        if ($null -ne $smokeProcess) {
            try {
                Stop-QuickAccessProcessTree -Process $smokeProcess
            } catch {
                $smokeCleanupFailure = $_
            }
        }
        try {
            Remove-Item -LiteralPath $smokeTempRoot -Recurse -Force -ErrorAction Stop
        } catch {
            if ($null -eq $smokeCleanupFailure) {
                $smokeCleanupFailure = $_
            }
        }
    }
    if ($null -ne $smokeFailure -or $null -ne $smokeCleanupFailure) {
        Remove-Item -LiteralPath $artifact -Force -ErrorAction SilentlyContinue
        if ($null -ne $smokeFailure) {
            throw $smokeFailure
        }
        throw $smokeCleanupFailure
    }
}

# Re-check provenance after every test/build hook.  A concurrent source edit or
# moved tag must never be recorded as a clean, tagged release.
$finalGitCommit = (& git -C $PSScriptRoot rev-parse HEAD 2>$null).Trim()
if ($LASTEXITCODE -ne 0 -or $finalGitCommit -notmatch '^[0-9a-fA-F]{40,64}$') {
    throw "Could not revalidate the final Git commit."
}
$finalGitStatus = @(& git -C $PSScriptRoot status --porcelain --untracked-files=normal)
if ($LASTEXITCODE -ne 0) {
    throw "Could not revalidate the final Git worktree."
}
$finalTagsAtHead = @(& git -C $PSScriptRoot tag --points-at HEAD)
if ($LASTEXITCODE -ne 0) {
    throw "Could not revalidate the final Git tags."
}
$finalSourceDirty = $finalGitStatus.Count -gt 0
$finalHasExpectedTag = $finalTagsAtHead -ccontains $expectedTag
if (-not $AllowDirty) {
    if (
        $finalGitCommit -cne $gitCommit -or
        $finalSourceDirty -or
        -not $finalHasExpectedTag
    ) {
        throw "Source commit, worktree, or release tag changed during the build. Discarding release eligibility."
    }
} else {
    $gitCommit = $finalGitCommit
    $sourceDirty = $finalSourceDirty
}

$signature = Get-AuthenticodeSignature -LiteralPath $artifact
$signatureStatus = $signature.Status.ToString()
& $PythonExecutable devtools\build_license_bundle.py `
    --output $licenseBundle `
    --version $projectVersion `
    --source-date-epoch $SourceDateEpoch
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $licenseBundle -PathType Leaf)) {
    throw "Third-party license bundle generation failed."
}
$sourceDirtyText = if ($sourceDirty) { "true" } else { "false" }
$releaseEligibleText = if ($AllowDirty) { "false" } else { "true" }
& $PythonExecutable devtools\release_manifest.py `
    --artifact $artifact `
    --output $manifest `
    --version $projectVersion `
    --signature-status $signatureStatus `
    --python-version $parsedVersion.ToString() `
    --pyinstaller-version $pyInstallerVersion `
    --source-date-epoch $SourceDateEpoch `
    --source-commit $gitCommit `
    --source-dirty $sourceDirtyText `
    --release-eligible $releaseEligibleText `
    --license-bundle $licenseBundle
if ($LASTEXITCODE -ne 0) {
    throw "Release manifest generation failed."
}

Write-Host "Built: $artifact"
Write-Host "Manifest: $manifest"
Write-Host "Licenses: $licenseBundle"
if ($signatureStatus -ne "Valid") {
    Write-Warning "Authenticode status is $signatureStatus. Sign before release or publish this unsigned status and SHA-256 honestly."
}

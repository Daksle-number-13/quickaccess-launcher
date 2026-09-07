<#
.SYNOPSIS
Behaviorally verifies the release smoke and signing preflight gates safely.
.DESCRIPTION
Uses a disposable Windows EXE, not the real application. Production smoke
code is parsed from build.ps1; only its root-directory binding is redirected
to the fixture. No build, real dist replacement, certificate signing, or
network access occurs. A JSON report remains under artifacts/audit-build.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$auditBase = [IO.Path]::GetFullPath((Join-Path $repoRoot 'artifacts\audit-build'))
$runId = [guid]::NewGuid().ToString('N')
$runRoot = [IO.Path]::GetFullPath((Join-Path $auditBase ("fixtures-$runId")))
$reportPath = Join-Path $auditBase ("release-script-results-$runId.json")
$expectedPrefix = $auditBase.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
if (-not $runRoot.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing an unexpected fixture path: $runRoot"
}
New-Item -ItemType Directory -Path $runRoot -Force | Out-Null

function Read-ReleaseScriptAst {
    param([string]$Path)
    $tokens = $null
    $parseErrors = $null
    $parsed = [Management.Automation.Language.Parser]::ParseFile(
        $Path, [ref]$tokens, [ref]$parseErrors
    )
    if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
    return $parsed
}

function Get-OwnedProbeProcesses {
    param([string]$CaseRoot)
    foreach ($pidFile in @(Get-ChildItem -LiteralPath $CaseRoot -Filter '*.pid')) {
        $probeId = [int](Get-Content -LiteralPath $pidFile.FullName -Raw)
        $probe = Get-Process -Id $probeId -ErrorAction SilentlyContinue
        if ($null -eq $probe) { continue }
        # Verify the exact image path as well as PID before any test cleanup.
        $expectedImage = Join-Path $CaseRoot 'QuickAccess.exe'
        if ($probe.Path -and [IO.Path]::GetFullPath($probe.Path) -ieq $expectedImage) {
            $probe
        } else {
            throw "Probe PID $probeId is not owned by this fixture; refusing to stop it."
        }
    }
}

$results = [Collections.Generic.List[object]]::new()
$caseDirectories = [Collections.Generic.List[string]]::new()
$originalAuditOutput = $env:QA_AUDIT_OUTPUT
$originalAuditMode = $env:QA_AUDIT_MODE
$fatalFailure = $null
try {
    $buildPath = Join-Path $repoRoot 'build.ps1'
    $signPath = Join-Path $repoRoot 'sign.ps1'
    $buildAst = Read-ReleaseScriptAst $buildPath
    $null = Read-ReleaseScriptAst $signPath
    $stopFunction = $buildAst.Find({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Stop-QuickAccessProcessTree'
    }, $true)
    $smokeIf = $buildAst.Find({
        param($node)
        $node -is [Management.Automation.Language.IfStatementAst] -and
        $node.Clauses[0].Item1.Extent.Text -eq '$SmokeTest'
    }, $true)
    if (-not $stopFunction -or -not $smokeIf) {
        throw 'Production smoke implementation could not be identified.'
    }
    . ([scriptblock]::Create($stopFunction.Extent.Text))
    $smokeBody = [scriptblock]::Create(
        $smokeIf.Extent.Text.Replace('$PSScriptRoot', '$caseRoot')
    )
    $helper = Join-Path $runRoot 'smoke-helper.exe'
    Add-Type -OutputAssembly $helper -OutputType WindowsApplication -TypeDefinition @'
using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
public static class QuickAccessReleaseScriptProbe {
    public static int Main(string[] args) {
        string output = Environment.GetEnvironmentVariable("QA_AUDIT_OUTPUT");
        if (args.Length > 0 && args[0] == "--child") {
            File.WriteAllText(Path.Combine(output, "child.pid"), Process.GetCurrentProcess().Id.ToString());
            Thread.Sleep(60000);
            return 0;
        }
        File.WriteAllText(Path.Combine(output, "root.pid"), Process.GetCurrentProcess().Id.ToString());
        string tempRoot = Path.GetTempPath();
        File.WriteAllText(Path.Combine(output, "temp.txt"), tempRoot);
        string expectedTempPrefix = Path.Combine(output, "build") + Path.DirectorySeparatorChar;
        if (!tempRoot.StartsWith(expectedTempPrefix, StringComparison.OrdinalIgnoreCase)) return 91;
        File.WriteAllText(Path.Combine(tempRoot, "probe.txt"), "isolated smoke artifact");
        string mode = Environment.GetEnvironmentVariable("QA_AUDIT_MODE");
        if (mode == "tree") {
            ProcessStartInfo child = new ProcessStartInfo(Process.GetCurrentProcess().MainModule.FileName, "--child");
            child.UseShellExecute = false;
            child.CreateNoWindow = true;
            Process.Start(child);
        }
        if (mode == "tree" || mode == "timeout") Thread.Sleep(60000);
        return mode == "failure" ? 7 : 0;
    }
}
'@

    foreach ($mode in @('success', 'failure', 'timeout', 'tree', 'start_failure')) {
        $caseRoot = Join-Path $runRoot $mode
        New-Item -ItemType Directory -Path $caseRoot | Out-Null
        $caseDirectories.Add($caseRoot)
        $artifact = Join-Path $caseRoot 'QuickAccess.exe'
        if ($mode -ne 'start_failure') { Copy-Item -LiteralPath $helper -Destination $artifact }
        $env:QA_AUDIT_OUTPUT = $caseRoot
        $env:QA_AUDIT_MODE = $mode
        $beforeTemp = $env:TEMP
        $beforeTmp = $env:TMP
        $SmokeTest = $true
        $SmokeTimeoutSeconds = 5
        $timer = [Diagnostics.Stopwatch]::StartNew()
        $failure = $null
        try { . $smokeBody } catch { $failure = $_.Exception.Message }
        $timer.Stop()
        $remaining = @(Get-ChildItem -LiteralPath (Join-Path $caseRoot 'build') -Directory -ErrorAction SilentlyContinue)
        $liveProbes = @(Get-OwnedProbeProcesses $caseRoot)
        $expectedFailure = $mode -ne 'success'
        $tempIsolated = $mode -eq 'start_failure'
        $observedTempFile = Join-Path $caseRoot 'temp.txt'
        if (Test-Path -LiteralPath $observedTempFile) {
            $observedTemp = (Get-Content -LiteralPath $observedTempFile -Raw).Trim()
            $caseTempPrefix = (Join-Path $caseRoot 'build') + [IO.Path]::DirectorySeparatorChar
            $tempIsolated = $observedTemp.StartsWith($caseTempPrefix, [StringComparison]::OrdinalIgnoreCase)
        }
        $checks = [ordered]@{
            expected_exit = (($null -ne $failure) -eq $expectedFailure)
            temp_isolated = $tempIsolated
            temp_restored = ($env:TEMP -ceq $beforeTemp -and $env:TMP -ceq $beforeTmp)
            no_temp_remains = ($remaining.Count -eq 0)
            no_owned_process_remains = ($liveProbes.Count -eq 0)
            artifact_state = ((Test-Path -LiteralPath $artifact) -eq (-not $expectedFailure))
            bounded_elapsed = ($timer.Elapsed.TotalSeconds -lt 15)
        }
        if ($mode -eq 'failure') {
            $checks.expected_exit = $failure -like '*exit code 7*'
        } elseif ($mode -in @('timeout', 'tree')) {
            $checks.expected_exit = $failure -like '*timed out after 5 seconds*'
        }
        $results.Add([pscustomobject]@{
            case="smoke_$mode"; passed=($checks.Values -notcontains $false)
            elapsed_seconds=[math]::Round($timer.Elapsed.TotalSeconds, 3)
            error=$failure; checks=$checks
        })
        foreach ($probe in $liveProbes) {
            Stop-Process -Id $probe.Id -Force -ErrorAction Stop
        }
    }

    # Execute the actual companion invalidation statements, redirected to a
    # fresh test dist, and verify that the old executable is not touched.
    $begin = $buildAst.EndBlock.Statements | Where-Object {
        $_.Extent.Text.StartsWith('$artifact = Join-Path')
    } | Select-Object -First 1
    $end = $buildAst.EndBlock.Statements | Where-Object {
        $_.Extent.Text.StartsWith('Remove-Item -LiteralPath $manifest, $licenseBundle')
    } | Select-Object -First 1
    if (-not $begin -or -not $end) { throw 'Stale companion gate could not be identified.' }
    $source = Get-Content -LiteralPath $buildPath -Raw
    $gateText = $source.Substring(
        $begin.Extent.StartOffset, $end.Extent.EndOffset - $begin.Extent.StartOffset
    ).Replace('$PSScriptRoot', '$runRoot')
    $projectVersion = '2.0.2'
    $fixtureDist = Join-Path $runRoot 'dist'
    New-Item -ItemType Directory -Path $fixtureDist | Out-Null
    foreach ($name in @('QuickAccess.exe', 'QuickAccess.release.json', 'QuickAccess_Third_Party_Licenses_v2.0.2.zip')) {
        [IO.File]::WriteAllText((Join-Path $fixtureDist $name), 'stale test fixture')
    }
    . ([scriptblock]::Create($gateText))
    $staleChecks = [ordered]@{
        executable_preserved = ((Get-Content -LiteralPath $artifact -Raw) -eq 'stale test fixture')
        manifest_removed = (-not (Test-Path -LiteralPath $manifest))
        license_removed = (-not (Test-Path -LiteralPath $licenseBundle))
    }
    $results.Add([pscustomobject]@{
        case='stale_companions'; passed=($staleChecks.Values -notcontains $false); checks=$staleChecks
    })

    foreach ($mode in @('bad_schema', 'dirty', 'not_release', 'exe_hash', 'missing_license', 'license_hash', 'traversal')) {
        $caseRoot = Join-Path $runRoot ("sign_$mode")
        New-Item -ItemType Directory -Path $caseRoot | Out-Null
        $exe = Join-Path $caseRoot 'QuickAccess.exe'
        $zip = Join-Path $caseRoot 'QuickAccess_Third_Party_Licenses_v2.0.2.zip'
        $json = Join-Path $caseRoot 'QuickAccess.release.json'
        [IO.File]::WriteAllText($exe, 'test-only executable fixture; never run')
        [IO.File]::WriteAllText($zip, 'test-only license fixture')
        $originalHash = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash
        $data = @{
            schema_version=2; product='QuickAccess Launcher'; version='2.0.2'
            artifact=@{name='QuickAccess.exe'; size=(Get-Item -LiteralPath $exe).Length; sha256=$originalHash}
            authenticode=@{status='NotSigned'; signed=$false}
            build=@{source_commit=('a' * 40); source_dirty=$false; release_eligible=$true}
            companion_artifacts=@(@{
                name='QuickAccess_Third_Party_Licenses_v2.0.2.zip'
                size=(Get-Item -LiteralPath $zip).Length
                sha256=(Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash
            })
        }
        switch ($mode) {
            'bad_schema' {$data.schema_version=1; $expected='schema-v2'}
            'dirty' {$data.build.source_dirty=$true; $expected='schema-v2'}
            'not_release' {$data.build.release_eligible=$false; $expected='schema-v2'}
            'exe_hash' {$data.artifact.sha256=('0' * 64); $expected='does not match the executable'}
            'missing_license' {Remove-Item -LiteralPath $zip; $expected='companion artifact is missing'}
            'license_hash' {$data.companion_artifacts[0].sha256=('0' * 64); $expected='hash mismatch'}
            'traversal' {$data.companion_artifacts[0].name='..\outside.zip'; $expected='Unsafe companion artifact name'}
        }
        [IO.File]::WriteAllText($json, ($data | ConvertTo-Json -Depth 6))
        $failure = $null
        try {
            & $signPath -CertificateThumbprint ('0' * 40) `
                -TimestampServer 'https://timestamp.example.invalid/' -Executable $exe
        } catch { $failure = $_.Exception.Message }
        $checks = [ordered]@{
            expected_rejection = [bool]($failure -and $failure.Contains($expected))
            executable_unchanged = ((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash -eq $originalHash)
        }
        $results.Add([pscustomobject]@{
            case="sign_$mode"; passed=($checks.Values -notcontains $false); error=$failure; checks=$checks
        })
    }
} catch {
    $fatalFailure = $_.Exception.Message
} finally {
    foreach ($caseDirectory in $caseDirectories) {
        try {
            foreach ($probe in @(Get-OwnedProbeProcesses $caseDirectory)) {
                Stop-Process -Id $probe.Id -Force -ErrorAction Stop
                if (-not $probe.WaitForExit(5000)) { throw 'Test probe did not exit during cleanup.' }
            }
        } catch {
            if (-not $fatalFailure) { $fatalFailure = $_.Exception.Message }
        }
    }
    if ($null -eq $originalAuditOutput) { Remove-Item Env:QA_AUDIT_OUTPUT -ErrorAction SilentlyContinue }
    else { $env:QA_AUDIT_OUTPUT = $originalAuditOutput }
    if ($null -eq $originalAuditMode) { Remove-Item Env:QA_AUDIT_MODE -ErrorAction SilentlyContinue }
    else { $env:QA_AUDIT_MODE = $originalAuditMode }
    try {
        $resolvedFixture = (Resolve-Path -LiteralPath $runRoot).Path
        if (-not $resolvedFixture.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove an unexpected fixture path: $resolvedFixture"
        }
        Remove-Item -LiteralPath $resolvedFixture -Recurse -Force -ErrorAction Stop
    } catch {
        if (-not $fatalFailure) { $fatalFailure = $_.Exception.Message }
    }
}

$passedCount = @($results | Where-Object { $_.passed }).Count
$report = [ordered]@{
    passed = ($passedCount -eq 13 -and $results.Count -eq 13 -and -not $fatalFailure)
    passed_cases = $passedCount; total_cases = $results.Count
    powershell_version = $PSVersionTable.PSVersion.ToString()
    build_script_sha256 = (Get-FileHash -LiteralPath (Join-Path $repoRoot 'build.ps1') -Algorithm SHA256).Hash
    sign_script_sha256 = (Get-FileHash -LiteralPath (Join-Path $repoRoot 'sign.ps1') -Algorithm SHA256).Hash
    fatal_error = $fatalFailure; fixture_cleanup_complete = (-not (Test-Path -LiteralPath $runRoot))
    results = @($results.ToArray())
}
[IO.File]::WriteAllText($reportPath, ($report | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
$results | Select-Object case, passed, elapsed_seconds | Format-Table -AutoSize
Write-Host "Release-script behavior checks: $passedCount/13 passed."
Write-Host "Report: $reportPath"
if (-not $report.passed) { throw "Release-script verification failed: $fatalFailure" }

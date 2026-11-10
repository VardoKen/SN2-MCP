[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string]$GameRoot = 'E:\Games\Subnautica 2',
    [string]$Executable,
    [string]$DllPath,
    [uint32]$ExpectedPid,
    [string]$ExpectedCreationTime,
    [string]$ExpectedDllSha256,
    [switch]$Json
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
. (Join-Path $PSScriptRoot 'game_process_common.ps1')
. (Join-Path $PSScriptRoot 'native_load_result.ps1')
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskLoader = Join-Path $taskRoot 'build\native\Release\sn2_mcp_loader.exe'
$taskDll = if ([string]::IsNullOrWhiteSpace($DllPath)) { Join-Path $taskRoot 'build\native\Release\SN2_MCP.dll' } else { $DllPath }
if (-not (Test-Path -LiteralPath $taskLoader -PathType Leaf) -or -not (Test-Path -LiteralPath $taskDll -PathType Leaf)) {
    throw 'Run tools/build_native.ps1 first'
}
$lease = $null
$dllPin = $null
$loadStarted = $false
$loadErrorCode = 'native_load_failed'
$nativeResult = $null
try {
    if (-not [IO.Path]::IsPathRooted($taskDll)) { throw 'DllPath must be absolute.' }
    $taskDll = (Resolve-Path -LiteralPath $taskDll -ErrorAction Stop).ProviderPath
    $selectedExecutable = if ([string]::IsNullOrWhiteSpace($Executable)) { Join-Path $GameRoot 'Subnautica2\Binaries\Win64\Subnautica2-Win64-Shipping.exe' } else { $Executable }
    if (-not [IO.Path]::IsPathRooted($selectedExecutable)) { throw 'Executable must be absolute.' }
    $expectedExe = (Resolve-Path -LiteralPath $selectedExecutable -ErrorAction Stop).ProviderPath
    $controlled = $ExpectedPid -ne 0 -or -not [string]::IsNullOrWhiteSpace($ExpectedCreationTime) -or -not [string]::IsNullOrWhiteSpace($ExpectedDllSha256)
    if ($controlled) {
        if ($ExpectedPid -eq 0 -or [string]::IsNullOrWhiteSpace($ExpectedCreationTime) -or $ExpectedDllSha256 -cnotmatch '^[0-9a-fA-F]{64}$') {
            throw 'Controlled loading requires ExpectedPid, ExpectedCreationTime, and ExpectedDllSha256 together.'
        }
    } else {
        $identities = @()
        foreach ($process in @(Get-Process -Name 'Subnautica2-Win64-Shipping' -ErrorAction SilentlyContinue)) {
            $identity = Get-SN2ProcessIdentity -ProcessId $process.Id
            if ($identity.executable -ieq $expectedExe) { $identities += $identity }
        }
        if ($identities.Count -ne 1) { throw 'No unique game process was found in the selected game directory.' }
        $ExpectedPid = $identities[0].pid
        $ExpectedCreationTime = $identities[0].creation_time
    }
    $lease = Open-SN2CheckedProcess -ExpectedPid $ExpectedPid -ExpectedCreationTime $ExpectedCreationTime -ExpectedExecutable $expectedExe
    $dllPin = [System.IO.File]::Open($taskDll, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try { $actualSha = ([BitConverter]::ToString($algorithm.ComputeHash($dllPin))).Replace('-', '').ToLowerInvariant() }
    finally { $algorithm.Dispose() }
    if ($controlled -and $actualSha -cne $ExpectedDllSha256.ToLowerInvariant()) { throw 'DLL SHA-256 changed; loading cancelled.' }
    if (-not $PSCmdlet.ShouldProcess("PID $ExpectedPid", 'Load the verified DLL through LoadLibraryW')) {
        $result = [pscustomobject]@{ state = 'not_started'; process = (ConvertTo-SN2ProcessIdentity $lease.Identity); dll_sha256 = $actualSha; outcome_unknown = $false }
    } else {
        $creationFileTime = ConvertTo-SN2CreationFileTime -CreationTime $ExpectedCreationTime
        $loadStarted = $true
        $nativeOutput = & $taskLoader --pid $ExpectedPid --dll $taskDll --expected-exe $expectedExe --expected-creation-filetime $creationFileTime
        if ($LASTEXITCODE -ne 0) {
            $nativeResult = ($nativeOutput -join "`n") | ConvertFrom-Json -ErrorAction Stop
            if ($nativeResult.ok -isnot [bool] -or $nativeResult.ok -ne $false -or
                $nativeResult.outcome_unknown -isnot [bool] -or $nativeResult.code -isnot [string]) {
                throw 'The native loader returned an invalid failure result.'
            }
            $loadStarted = $nativeResult.outcome_unknown
            $loadErrorCode = $nativeResult.code
            throw 'The native loader did not confirm loading; inspect native_result.'
        }
        $nativeResult = ConvertFrom-SN2NativeLoadResult -Source ($nativeOutput -join "`n") -ExpectedPid $ExpectedPid
        $result = [pscustomobject]@{ state = $nativeResult.status; process = (ConvertTo-SN2ProcessIdentity $lease.Identity); dll_path = $taskDll; dll_sha256 = $actualSha; native_result = $nativeResult; outcome_unknown = $false }
    }
    if ($Json) { $result | ConvertTo-Json -Depth 6 -Compress } else { $result }
} catch {
    if ($Json) {
        [pscustomobject]@{ state = $(if ($loadStarted) { 'outcome_unknown' } else { 'failed' }); code = $loadErrorCode;
            message = 'DLL loading into the original process was not confirmed.'; native_result = $nativeResult; outcome_unknown = $loadStarted } | ConvertTo-Json -Depth 6 -Compress
        exit 2
    }
    throw
} finally {
    if ($null -ne $dllPin) { $dllPin.Dispose() }
    if ($null -ne $lease) { $lease.Dispose() }
}

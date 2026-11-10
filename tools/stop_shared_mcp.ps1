<#
.SYNOPSIS
Stop only the recorded shared MCP server process.
.DESCRIPTION
Checks the command line, PID, creation time, and EXE; terminates the process
through its retained HANDLE. Another process never becomes the stop target.
#>
[CmdletBinding()]
param(
    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$Json
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'start_shared_mcp.ps1') -RepositoryRoot $RepositoryRoot -Json:$Json

function Invoke-G1RSharedStop {
    param($Configuration)
    $record = Read-G1RSharedManifest $Configuration
    if ($null -eq $record) {
        Assert-G1RSharedPort $Configuration @(Get-G1RSharedListeners $Configuration)
        Assert-G1RSharedNoUntrackedProcess $Configuration
        return [pscustomobject]@{ state = 'already_stopped'; process = $null }
    }
    $launcherLease = Open-G1RSharedLauncher $Configuration $record -ForTermination
    $lease = $null
    try {
        $lease = Open-G1RSharedProcess $Configuration $record -ForTermination
        if ($null -eq $lease) {
            Assert-G1RSharedPort $Configuration @(Get-G1RSharedListeners $Configuration)
            if ($null -ne $launcherLease) {
                $null = Stop-G1RCheckedProcess -Lease $launcherLease -GraceMilliseconds 0 -ForceWaitMilliseconds 5000 -AllowForce
            }
            Remove-Item -LiteralPath $Configuration.manifest -Force
            return [pscustomobject]@{ state = 'already_exited'; process = $record }
        }
        Assert-G1RSharedPort $Configuration @(Get-G1RSharedListeners $Configuration) $record.pid
        $stopped = Stop-G1RCheckedProcess -Lease $lease -GraceMilliseconds 0 -ForceWaitMilliseconds 5000 -AllowForce
        if ($null -ne $launcherLease -and $record.launcher.pid -ne $record.pid) {
            $null = Stop-G1RCheckedProcess -Lease $launcherLease -GraceMilliseconds 0 -ForceWaitMilliseconds 5000 -AllowForce
        }
        Remove-Item -LiteralPath $Configuration.manifest -Force
        return [pscustomobject]@{ state = $stopped.state; process = $record; stop_mode = $stopped.mode }
    } finally {
        if ($null -ne $lease) { $lease.Dispose() }
        if ($null -ne $launcherLease -and $launcherLease -ne $lease) { $launcherLease.Dispose() }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        $configuration = Get-G1RSharedConfiguration $RepositoryRoot
        $result = Invoke-G1RSharedLocked { Invoke-G1RSharedStop $configuration }
        if ($Json) { $result | ConvertTo-Json -Depth 6 } else { $result }
    } catch {
        $systemFailure = $_.Exception.GetBaseException()
        if ($_.Exception.Message -cmatch '^shared_[a-z_]+: ') {
            [Console]::Error.WriteLine($_.Exception.Message)
        } elseif ($systemFailure.GetType().FullName -ceq 'G1RMCP.ProcessControl.ProcessControlException') {
            [Console]::Error.WriteLine($systemFailure.Message)
        } else {
            $message = 'The shared MCP server stop operation failed (hresult={0})' -f $systemFailure.HResult
            if ($systemFailure -is [ComponentModel.Win32Exception]) {
                $message += ', winerror={0}' -f $systemFailure.NativeErrorCode
            }
            [Console]::Error.WriteLine($message)
        }
        exit 2
    }
}

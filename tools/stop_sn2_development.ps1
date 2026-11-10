[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string]$GameRoot = 'E:\Games\Subnautica 2',
    [Parameter(Mandatory = $true)][uint32]$ExpectedPid,
    [Parameter(Mandatory = $true)][string]$ExpectedCreationTime,
    [switch]$Json
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
. (Join-Path $PSScriptRoot 'game_process_common.ps1')

function Get-SN2StopSnapshot {
    param([string]$Inner, [string]$Bootstrap)
    $items = @()
    foreach ($candidate in @(Get-CimInstance Win32_Process -Filter "Name='Subnautica2.exe' OR Name='Subnautica2-Win64-Shipping.exe'")) {
        $identity = Get-SN2ProcessIdentity -ProcessId $candidate.ProcessId
        $fresh = @(Get-CimInstance Win32_Process -Filter ("ProcessId=" + $candidate.ProcessId))
        if ($fresh.Count -ne 1 -or $null -eq $candidate.CreationDate -or $null -eq $fresh[0].CreationDate) {
            throw 'Could not associate the parent PID with an exact process identity.'
        }
        $created = [DateTime]::ParseExact($identity.creation_time, "yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'",
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::AssumeUniversal -bor [System.Globalization.DateTimeStyles]::AdjustToUniversal)
        if ($fresh[0].ProcessId -ne $candidate.ProcessId -or
            $fresh[0].ExecutablePath -ine $identity.executable -or
            $fresh[0].ParentProcessId -ne $candidate.ParentProcessId -or
            $fresh[0].CreationDate -ne $candidate.CreationDate -or
            [Math]::Abs(($fresh[0].CreationDate.ToUniversalTime() - $created).TotalMilliseconds) -gt 1) {
            throw 'The parent PID snapshot refers to a changed process.'
        }
        if ($identity.executable -ine $Inner -and $identity.executable -ine $Bootstrap) { continue }
        $items += [pscustomobject]@{ identity = $identity; parent_pid = [uint32]$fresh[0].ParentProcessId }
    }
    if (@($items | Where-Object { $_.identity.executable -ieq $Inner }).Count -gt 1 -or
        @($items | Where-Object { $_.identity.executable -ieq $Bootstrap }).Count -gt 1) {
        throw 'Multiple instances of the same game EXE were found.'
    }
    return $items
}

function Test-SN2StopChild {
    param($Child, $ParentIdentity, [string]$ParentExitTime)
    if ($Child.parent_pid -ne $ParentIdentity.pid) { return $false }
    # Strings in the same exact UTC format compare in chronological order.
    if ([string]::CompareOrdinal($Child.identity.creation_time, $ParentIdentity.creation_time) -lt 0) { return $false }
    if (-not [string]::IsNullOrEmpty($ParentExitTime) -and
        [string]::CompareOrdinal($Child.identity.creation_time, $ParentExitTime) -gt 0) { return $false }
    return $true
}

$lease = $null
$bootstrapLease = $null
$innerLease = $null
$actionStarted = $false
$cleanup = @()
$selectedIdentity = $null
try {
    $ExpectedCreationTime = ConvertTo-SN2CreationTimeUtc -CreationTime $ExpectedCreationTime
    $resolvedRoot = (Resolve-Path -LiteralPath $GameRoot -ErrorAction Stop).ProviderPath
    $inner = (Resolve-Path -LiteralPath (Join-Path $resolvedRoot 'Subnautica2\Binaries\Win64\Subnautica2-Win64-Shipping.exe') -ErrorAction Stop).ProviderPath
    $bootstrap = (Resolve-Path -LiteralPath (Join-Path $resolvedRoot 'Subnautica2.exe') -ErrorAction Stop).ProviderPath
    $snapshot = @(Get-SN2StopSnapshot -Inner $inner -Bootstrap $bootstrap)
    $selected = @($snapshot | Where-Object {
        $_.identity.pid -eq $ExpectedPid -and $_.identity.creation_time -ceq $ExpectedCreationTime
    })
    if ($selected.Count -ne 1) { throw 'The expected game process identity changed.' }
    $selectedIdentity = $selected[0].identity
    $lease = Open-SN2CheckedProcess -ExpectedPid $ExpectedPid -ExpectedCreationTime $ExpectedCreationTime `
        -ExpectedExecutable $selectedIdentity.executable -ForTermination
    $selectedIsBootstrap = $selectedIdentity.executable -ieq $bootstrap
    if ($selectedIsBootstrap) {
        $bootstrapLease = $lease
        $candidateInner = @($snapshot | Where-Object { $_.identity.executable -ieq $inner })
        if ($candidateInner.Count -eq 1) {
            if (-not (Test-SN2StopChild -Child $candidateInner[0] -ParentIdentity $selectedIdentity)) {
                throw 'The internal process does not belong to the selected bootstrapper.'
            }
            $identity = $candidateInner[0].identity
            $innerLease = Open-SN2CheckedProcess -ExpectedPid $identity.pid -ExpectedCreationTime $identity.creation_time `
                -ExpectedExecutable $inner -ForTermination
        }
    } else {
        $innerLease = $lease
        $candidateBootstrap = @($snapshot | Where-Object { $_.identity.executable -ieq $bootstrap })
        if ($candidateBootstrap.Count -eq 1) {
            if (-not (Test-SN2StopChild -Child $selected[0] -ParentIdentity $candidateBootstrap[0].identity)) {
                throw 'The bootstrapper is not the parent of the selected internal process.'
            }
            $identity = $candidateBootstrap[0].identity
            $bootstrapLease = Open-SN2CheckedProcess -ExpectedPid $identity.pid -ExpectedCreationTime $identity.creation_time `
                -ExpectedExecutable $bootstrap -ForTermination
        }
    }
    if (-not $PSCmdlet.ShouldProcess("PID $ExpectedPid", 'Terminate the exact process of the selected game session')) {
        $result = [pscustomobject]@{ state = 'not_started'; process = $selectedIdentity; cleanup = @(); outcome_unknown = $false }
    } else {
        $actionStarted = $true
        # Stop the original bootstrapper first: after its HANDLE signals, it cannot create new children.
        if ($null -ne $bootstrapLease) {
            $cleanup += Stop-SN2CheckedProcess -Lease $bootstrapLease -GraceMilliseconds 1000 -ForceWaitMilliseconds 3000 -AllowForce
        }
        if ($selectedIsBootstrap) {
            $exitTime = $bootstrapLease.GetExitTime()
            $parentIdentity = ConvertTo-SN2ProcessIdentity $bootstrapLease.Identity
            # A late child is allowed only within the confirmed lifetime of the retained parent.
            $deadline = [System.Diagnostics.Stopwatch]::StartNew()
            do {
                $lateSnapshot = @(Get-SN2StopSnapshot -Inner $inner -Bootstrap $bootstrap)
                $lateInner = @($lateSnapshot | Where-Object { $_.identity.executable -ieq $inner })
                $lateBootstrap = @($lateSnapshot | Where-Object { $_.identity.executable -ieq $bootstrap })
                if ($lateBootstrap.Count -gt 0) { throw 'Another bootstrapper appeared after shutdown; further cleanup cancelled.' }
                if ($lateInner.Count -eq 1) {
                    if (-not (Test-SN2StopChild -Child $lateInner[0] -ParentIdentity $parentIdentity -ParentExitTime $exitTime)) {
                        throw 'The late internal process falls outside the original bootstrapper lifetime.'
                    }
                    $identity = $lateInner[0].identity
                    if ($null -eq $innerLease) {
                        $innerLease = Open-SN2CheckedProcess -ExpectedPid $identity.pid -ExpectedCreationTime $identity.creation_time `
                            -ExpectedExecutable $inner -ForTermination
                    } elseif ($innerLease.Identity.Pid -ne $identity.pid -or
                        $innerLease.Identity.CreationTime -cne $identity.creation_time) {
                        throw 'The internal process was replaced; further cleanup cancelled.'
                    }
                    break
                }
                if ($null -ne $innerLease -or $deadline.ElapsedMilliseconds -ge 750) { break }
                Start-Sleep -Milliseconds 50
            } while ($true)
        }
        if ($null -ne $innerLease) {
            $cleanup += Stop-SN2CheckedProcess -Lease $innerLease -GraceMilliseconds 15000 -ForceWaitMilliseconds 3000 -AllowForce
        }
        $remaining = @(Get-SN2StopSnapshot -Inner $inner -Bootstrap $bootstrap)
        if ($remaining.Count -ne 0) { throw 'Game processes were found after the original HANDLEs terminated; cleanup is incomplete.' }
        $mode = if ($cleanup.Count -gt 0) { $cleanup[0].mode } else { 'none' }
        $result = [pscustomobject]@{ state = 'stopped'; mode = $mode; process = $selectedIdentity;
            cleanup = @($cleanup); outcome_unknown = $false }
    }
    if ($Json) { $result | ConvertTo-Json -Depth 7 -Compress } else { $result }
} catch {
    if ($Json) {
        [pscustomobject]@{ state = 'failed'; code = 'session_cleanup_incomplete';
            message = 'Termination of the original game session was not confirmed.';
            process = $selectedIdentity; cleanup = @($cleanup); outcome_unknown = $actionStarted } | ConvertTo-Json -Depth 7 -Compress
        exit 2
    }
    throw
} finally {
    if ($null -ne $innerLease -and $innerLease -ne $lease) { $innerLease.Dispose() }
    if ($null -ne $bootstrapLease -and $bootstrapLease -ne $lease) { $bootstrapLease.Dispose() }
    if ($null -ne $lease) { $lease.Dispose() }
}

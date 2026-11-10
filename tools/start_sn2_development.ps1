<#
.SYNOPSIS
Launch the selected Subnautica 2 bootstrap executable.
.DESCRIPTION
Uses the shipped bootstrap executable without Gothic mod or AngelScript flags.
A restart checks the existing PID, creation time and executable before closure.
Does not change game files, configuration, saves or global launch settings.
.EXAMPLE
.\tools\start_sn2_development.ps1 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string]$GameRoot = 'E:\Games\Subnautica 2',
    [uint32]$ExpectedPid,
    [string]$ExpectedCreationTime,
    [switch]$Json,
    [switch]$Restart
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
. (Join-Path $PSScriptRoot 'game_process_common.ps1')

function Get-RequiredPath {
    param([string]$Path, [string]$Label, [switch]$Directory)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "$Label`: path is not specified."
    }
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    }
    catch {
        throw "$Label`: path was not found or is inaccessible."
    }
    if ($item.PSProvider.Name -ne 'FileSystem' -or
        $item.PSIsContainer -ne [bool]$Directory) {
        throw "$Label`: unsuitable path type."
    }
    return [System.IO.Path]::GetFullPath($item.FullName)
}

function Get-CheckedGameProcesses {
    param([string]$BootstrapPath, [string]$InnerPath)

    $processes = @(Get-CimInstance Win32_Process -Filter "Name='Subnautica2.exe' OR Name='Subnautica2-Win64-Shipping.exe'" | Where-Object {
        $_.ExecutablePath -ieq $BootstrapPath -or $_.ExecutablePath -ieq $InnerPath
    })
    foreach ($process in $processes) {
        if ([string]::IsNullOrWhiteSpace($process.ExecutablePath) -or
            ($process.ExecutablePath -ine $BootstrapPath -and
             $process.ExecutablePath -ine $InnerPath)) {
            throw 'The selected game process path changed; launch and shutdown cancelled.'
        }
    }
    foreach ($allowedPath in @($BootstrapPath, $InnerPath)) {
        if (@($processes | Where-Object { $_.ExecutablePath -ieq $allowedPath }).Count -gt 1) {
            throw 'Multiple game or bootstrapper instances were found; launch and shutdown cancelled.'
        }
    }
    return $processes
}

function Assert-NoPathRedirect {
    param([string]$Path)
    $current = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    while ($null -ne $current) {
        if (($current.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Game path contains a symlink or junction; launch cancelled.'
        }
        if ($current -is [System.IO.DirectoryInfo]) {
            $current = $current.Parent
        } else {
            $current = $current.Directory
        }
    }
}

$resolvedGameRoot = Get-RequiredPath -Path $GameRoot -Label 'Game directory' -Directory
$bootstrapPath = Get-RequiredPath -Path (Join-Path $resolvedGameRoot 'Subnautica2.exe') -Label 'Game bootstrapper'
$innerPath = Get-RequiredPath -Path (Join-Path $resolvedGameRoot 'Subnautica2\Binaries\Win64\Subnautica2-Win64-Shipping.exe') -Label 'Game EXE'
$runningProcesses = @(Get-CheckedGameProcesses -BootstrapPath $bootstrapPath -InnerPath $innerPath)
if ($runningProcesses.Count -gt 0 -and -not $Restart) {
    throw 'The game is already running. Use -Restart for a graceful restart.'
}

$operation = if ($runningProcesses.Count -gt 0) {
    'Close the selected game session and launch Subnautica 2'
} else {
    'Launch Subnautica 2'
}
if (-not $PSCmdlet.ShouldProcess($resolvedGameRoot, $operation)) {
    $result = [pscustomobject]@{ state = 'not_started'; executable = $bootstrapPath; arguments = @(); restart_requested = [bool]$Restart; outcome_unknown = $false }
    if ($Json) { $result | ConvertTo-Json -Depth 5 -Compress } else { $result }
    return
}
# Recheck state after any wait for confirmation.
$runningProcesses = @(Get-CheckedGameProcesses -BootstrapPath $bootstrapPath -InnerPath $innerPath)
if ($runningProcesses.Count -gt 0 -and -not $Restart) {
    throw 'The game started after the initial check; duplicate launch cancelled.'
}

if ($runningProcesses.Count -gt 0 -and ($ExpectedPid -ne 0 -or -not [string]::IsNullOrWhiteSpace($ExpectedCreationTime))) {
    if (-not $Restart -or $ExpectedPid -eq 0 -or [string]::IsNullOrWhiteSpace($ExpectedCreationTime)) {
        throw 'Controlled restart requires Restart, ExpectedPid, and ExpectedCreationTime together.'
    }
    $stoppedJson = & (Join-Path $PSScriptRoot 'stop_sn2_development.ps1') -GameRoot $resolvedGameRoot -ExpectedPid $ExpectedPid -ExpectedCreationTime $ExpectedCreationTime -Json -Confirm:$false
    $stopped = $stoppedJson | ConvertFrom-Json -ErrorAction Stop
    if ($stopped.state -ne 'stopped') { throw 'Termination of the original session before launch was not confirmed.' }
    $runningProcesses = @(Get-CheckedGameProcesses -BootstrapPath $bootstrapPath -InnerPath $innerPath)
} elseif ($runningProcesses.Count -eq 0 -and ($ExpectedPid -ne 0 -or -not [string]::IsNullOrWhiteSpace($ExpectedCreationTime))) {
    throw 'The expected game session has already ended; new launch cancelled.'
}
if ($runningProcesses.Count -gt 0) {
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    $originalProcessIds = @($runningProcesses | ForEach-Object { $_.ProcessId })
    $innerProcesses = @($runningProcesses | Where-Object { $_.ExecutablePath -ieq $innerPath })
    foreach ($process in $innerProcesses) {
        $handle = Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
        if ($null -eq $handle) {
            continue
        }
        $fresh = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $process.ProcessId)
        if ($null -eq $fresh -or $fresh.ExecutablePath -ine $innerPath -or
            $fresh.CreationDate -ne $process.CreationDate -or
            [Math]::Abs(($handle.StartTime.ToUniversalTime() - $process.CreationDate.ToUniversalTime()).TotalMilliseconds) -gt 1) {
            throw 'Game process identity changed; shutdown and launch cancelled.'
        }
        if (-not $handle.CloseMainWindow()) {
            throw 'The game did not accept graceful window closure; forced termination will not be performed.'
        }
    }

    # The bootstrapper usually exits after the game process. If the game has
    # not appeared yet, only graceful window closure can be sent to the bootstrapper.
    if ($innerProcesses.Count -eq 0) {
        foreach ($process in $runningProcesses) {
            $handle = Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
            if ($null -ne $handle) {
                $fresh = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $process.ProcessId)
                if ($null -eq $fresh -or $fresh.ExecutablePath -ine $bootstrapPath -or
                    $fresh.CreationDate -ne $process.CreationDate -or
                    [Math]::Abs(($handle.StartTime.ToUniversalTime() - $process.CreationDate.ToUniversalTime()).TotalMilliseconds) -gt 1) {
                    throw 'Bootstrapper identity changed; shutdown and launch cancelled.'
                }
                if (-not $handle.CloseMainWindow()) {
                    throw 'The bootstrapper did not accept graceful closure; launch cancelled.'
                }
            }
        }
    }

    do {
        $remaining = @(Get-CheckedGameProcesses -BootstrapPath $bootstrapPath -InnerPath $innerPath)
        if (@($remaining | Where-Object { $_.ProcessId -notin $originalProcessIds }).Count -gt 0) {
            throw 'A new game process appeared during shutdown; new launch cancelled.'
        }
        if ($remaining.Count -eq 0) {
            break
        }
        $milliseconds = [Math]::Min(200, [Math]::Max(0, ($deadline - [DateTime]::UtcNow).TotalMilliseconds))
        if ($milliseconds -gt 0) {
            Start-Sleep -Milliseconds ([int]$milliseconds)
        }
    } while ([DateTime]::UtcNow -lt $deadline)

    if (@(Get-CheckedGameProcesses -BootstrapPath $bootstrapPath -InnerPath $innerPath).Count -gt 0) {
        throw 'The game or bootstrapper did not exit within 20 seconds; new launch cancelled.'
    }
}

if (@(Get-CheckedGameProcesses -BootstrapPath $bootstrapPath -InnerPath $innerPath).Count -gt 0) {
    throw 'The game appeared before launch; duplicate launch cancelled.'
}
$request = Start-Process -FilePath $bootstrapPath -WorkingDirectory $resolvedGameRoot -WindowStyle Hidden -PassThru
try {
    $result = [pscustomobject]@{
        state = 'launch_requested'; executable = $bootstrapPath; arguments = @()
        bootstrap_pid = $request.Id; outcome_unknown = $false
        note = 'The bootstrap request does not establish a ready game executor; rediscover the actual internal process.'
    }
    if ($Json) { $result | ConvertTo-Json -Depth 5 -Compress } else { $result }
} finally { $request.Dispose() }

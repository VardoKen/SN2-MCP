<#
.SYNOPSIS
Launches Gothic 1 Remake in development mode through Steam.

.DESCRIPTION
Preserves the observed -FromModManagementUI route and adds one-time
AngelScript parameters. Does not change Steam settings, Mods.ini, cache, or
game files. The command line does not confirm actual mod activation.

Without -Restart, a running game causes rejection. -Restart gracefully closes
only the verified game process and waits for the bootstrapper to exit;
forced termination is not used without ExpectedPid and ExpectedCreationTime.
With exact identity, controlled restart uses a retained HANDLE and can
terminate the original session after a bounded wait. -WhatIf does not close
the game or send a launch request to Steam.

.EXAMPLE
.\tools\start_g1r_development.ps1 -Restart -WhatIf

.EXAMPLE
.\tools\start_g1r_development.ps1 -Restart
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string]$SteamExe,
    [string]$GameRoot = 'E:\SteamLibrary\steamapps\common\Gothic 1 Remake',
    [string]$AppId = '1297900',
    [string]$ModsDirectory,
    [string[]]$Mods,
    [string]$ModsCsv,
    [string]$ScriptDirectory,
    [switch]$ShowScriptErrors,

    [switch]$EnableIngameTools,
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

    $processes = @(Get-CimInstance Win32_Process -Filter "Name='G1R-Win64-Shipping.exe'")
    foreach ($process in $processes) {
        if ([string]::IsNullOrWhiteSpace($process.ExecutablePath) -or
            ($process.ExecutablePath -ine $BootstrapPath -and
             $process.ExecutablePath -ine $InnerPath)) {
            throw 'A game process with an unverified path was found; launch and shutdown cancelled.'
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
            throw 'Package path contains a symlink or junction; launch cancelled.'
        }
        if ($current -is [System.IO.DirectoryInfo]) {
            $current = $current.Parent
        } else {
            $current = $current.Directory
        }
    }
}

if ($AppId -cne '1297900') {
    throw 'This tool supports only Steam AppID 1297900.'
}

$resolvedGameRoot = Get-RequiredPath -Path $GameRoot -Label 'Game directory' -Directory
$bootstrapPath = Get-RequiredPath -Path (Join-Path $resolvedGameRoot 'G1R-Win64-Shipping.exe') -Label 'Game bootstrapper'
$innerPath = Get-RequiredPath -Path (Join-Path $resolvedGameRoot 'G1R\Binaries\Win64\G1R-Win64-Shipping.exe') -Label 'Game EXE'

if ([string]::IsNullOrWhiteSpace($SteamExe)) {
    try {
        $steamSettings = Get-ItemProperty -LiteralPath 'HKCU:\Software\Valve\Steam'
        $steamRoot = [string]$steamSettings.SteamPath
    }
    catch {
        throw 'Steam path was not found in the registry; specify -SteamExe explicitly.'
    }
    if ([string]::IsNullOrWhiteSpace($steamRoot)) {
        throw 'Steam path was not found in the registry; specify -SteamExe explicitly.'
    }
    $SteamExe = Join-Path $steamRoot 'steam.exe'
}
$resolvedSteamExe = Get-RequiredPath -Path $SteamExe -Label 'Steam EXE'
if ([System.IO.Path]::GetFileName($resolvedSteamExe) -ine 'steam.exe') {
    throw 'The steam.exe file is required for launch.'
}

$packageOptions = @()
$scriptOptions = @()
if (-not [string]::IsNullOrWhiteSpace($ScriptDirectory)) {
    if ($ScriptDirectory -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\[^\\]+)' -or $ScriptDirectory -match '["\x00-\x1f]' -or $ScriptDirectory.Substring(2).Contains(':')) {
        throw 'ScriptDirectory must be an absolute path without quotes or control characters.'
    }
    $resolvedScriptDirectory = Get-RequiredPath -Path $ScriptDirectory -Label 'Base Script directory' -Directory
    Assert-NoPathRedirect -Path $resolvedScriptDirectory
    $scriptOptions = @(('-ScriptDirectory="' + $resolvedScriptDirectory.Replace('\', '/') + '"'))
}
$resolvedModsDirectory = $null
$selectedMods = @($Mods | Where-Object { $null -ne $_ })
if ($PSBoundParameters.ContainsKey('ModsCsv')) {
    if ($PSBoundParameters.ContainsKey('Mods')) {
        throw 'ModsCsv and Mods cannot be specified together.'
    }
    if ([string]::IsNullOrWhiteSpace($ModsCsv)) {
        throw 'ModsCsv must contain one or more comma-separated package names.'
    }
    $selectedMods = @($ModsCsv.Split([char]','))
    if ($selectedMods.Count -lt 1) {
        throw 'ModsCsv must contain one or more comma-separated package names.'
    }
    foreach ($mod in $selectedMods) {
        if ($mod -cnotmatch '\A[A-Za-z0-9_-]+\z') {
            throw 'ModsCsv contains an invalid package name.'
        }
    }
}
if (-not [string]::IsNullOrWhiteSpace($ModsDirectory) -or $selectedMods.Count -gt 0) {
    if ([string]::IsNullOrWhiteSpace($ModsDirectory) -or $selectedMods.Count -eq 0) {
        throw 'ModsDirectory and Mods must be specified together.'
    }
    if ($ModsDirectory -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\[^\\]+)' -or $ModsDirectory -match '["\x00-\x1f]' -or $ModsDirectory.Substring(2).Contains(':')) {
        throw 'ModsDirectory must be an absolute path without quotes or control characters.'
    }
    $resolvedModsDirectory = Get-RequiredPath -Path $ModsDirectory -Label 'Package directory' -Directory
    Assert-NoPathRedirect -Path $resolvedModsDirectory
    $uniqueMods = @{}
    foreach ($mod in $selectedMods) {
        if ([string]::IsNullOrWhiteSpace($mod) -or $mod -cnotmatch '^[A-Za-z0-9_-]+$' -or $uniqueMods.ContainsKey($mod)) {
            throw 'Mods names must be unique directory names containing letters, digits, _ or -.'
        }
        $uniqueMods[$mod] = $true
        $packagePath = Get-RequiredPath -Path (Join-Path $resolvedModsDirectory $mod) -Label 'Package' -Directory
        Assert-NoPathRedirect -Path $packagePath
        $descriptorPath = Get-RequiredPath -Path (Join-Path $packagePath 'GothicMod.json') -Label 'Package descriptor'
        Assert-NoPathRedirect -Path $descriptorPath
        $descriptor = Get-Content -LiteralPath $descriptorPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $descriptor -or $null -eq $descriptor.PSObject.Properties['name'] -or $descriptor.name -cne $mod) {
            throw 'The name in GothicMod.json must match the selected Mods directory.'
        }
    }
    $quotedModsDirectory = $resolvedModsDirectory.Replace('\', '/')
    $packageOptions = @(('-ModsDirectory="' + $quotedModsDirectory + '"'), ('-Mods="' + ($selectedMods -join ',') + '"'))
}

$runningProcesses = @(Get-CheckedGameProcesses -BootstrapPath $bootstrapPath -InnerPath $innerPath)
if ($runningProcesses.Count -gt 0 -and -not $Restart) {
    throw 'The game is already running. Use -Restart for a graceful restart.'
}

$steamArguments = @(
    '-applaunch', $AppId,
    '-FromModManagementUI',
    '-as-development-mode',
    '-as-ignore-precompiled-data'
)
if (-not $ShowScriptErrors) { $steamArguments += '-as-exit-on-error' }
if ($EnableIngameTools) { $steamArguments += '-ingametools' }
$steamArguments += $packageOptions + $scriptOptions
$operation = if ($runningProcesses.Count -gt 0) {
    'Gracefully close the game and launch development mode through Steam'
}
else {
    'Launch development mode through Steam'
}

if (-not $PSCmdlet.ShouldProcess($resolvedGameRoot, $operation)) {
    $result = [pscustomobject]@{
        State = 'No state change'
        package_name = $(if ($selectedMods.Count -eq 1) { $selectedMods[0] } else { $null })
        mods = @($selectedMods)
        mods_directory = $resolvedModsDirectory
        SteamExe = $resolvedSteamExe
        AppId = $AppId
        Arguments = $steamArguments
        RestartRequested = [bool]$Restart
    }
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
    $stoppedJson = & (Join-Path $PSScriptRoot 'stop_g1r_development.ps1') -GameRoot $resolvedGameRoot -ExpectedPid $ExpectedPid -ExpectedCreationTime $ExpectedCreationTime -Json -Confirm:$false
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
    throw 'The game appeared before Steam launch; duplicate launch cancelled.'
}
$request = Start-Process -FilePath $resolvedSteamExe -ArgumentList $steamArguments -WindowStyle Hidden -PassThru
$result = [pscustomobject]@{
    state = 'launch_requested'
    package_name = $(if ($selectedMods.Count -eq 1) { $selectedMods[0] } else { $null })
    mods = @($selectedMods)
    mods_directory = $resolvedModsDirectory
    description = 'Launch request sent to Steam'
    SteamExe = $resolvedSteamExe
    AppId = $AppId
    Arguments = $steamArguments
    SteamRequestPid = $request.Id
    Note = 'PID belongs to the Steam request; game loading and mod activation still require verification.'
}
if ($Json) { $result | ConvertTo-Json -Depth 5 -Compress } else { $result }

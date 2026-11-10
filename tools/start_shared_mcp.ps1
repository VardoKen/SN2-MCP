<#
.SYNOPSIS
Start one shared MCP server at 127.0.0.1:27224.
.DESCRIPTION
Repeated launch checks the recorded process. A port owned by another process
or changed identity is an error. Does not start the game or create autostart.
#>
[CmdletBinding()]
param(
    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot),
    [ValidateRange(1, 60)][int]$StartupTimeoutSeconds = 20,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'game_process_common.ps1')

function Get-SN2SharedConfiguration {
    param([string]$Root)
    $resolved = (Resolve-Path -LiteralPath $Root).ProviderPath
    $artifactRoot = Join-Path $resolved 'artifacts'
    foreach ($path in @($resolved, $artifactRoot)) {
        if (Test-Path -LiteralPath $path) {
            if (((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'shared_unsafe_path: server directory is a symlink.'
            }
        }
    }
    $python = Join-Path $resolved '.venv\Scripts\python.exe'
    $basePython = $python
    $venvConfiguration = Join-Path $resolved '.venv\pyvenv.cfg'
    if (Test-Path -LiteralPath $venvConfiguration -PathType Leaf) {
        $homes = @(Get-Content -LiteralPath $venvConfiguration -Encoding UTF8 | Where-Object { $_ -match '^home\s*=\s*(.+)$' })
        if ($homes.Count -ne 1 -or $homes[0] -notmatch '^home\s*=\s*(.+)$') { throw 'shared_invalid_venv: base Python is not defined.' }
        $basePython = [IO.Path]::GetFullPath((Join-Path $Matches[1].Trim() 'python.exe'))
    }
    $arguments = '-B -m sn2_mcp.server --transport streamable-http --host 127.0.0.1 --port 27224 --workspace-root "' + $resolved + '" --native-port 27223 --runtime-root "' + $resolved + '"'
    [pscustomobject]@{
        root = $resolved; artifacts = $artifactRoot; python = $python
        base_python = $basePython
        script = Join-Path $resolved 'src\sn2_mcp\server.py'; arguments = $arguments
        command_line = '"' + $python + '" ' + $arguments
        manifest = Join-Path $artifactRoot 'sharedserver.json'
        stdout = Join-Path $artifactRoot 'sharedserver.stdout.log'
        stderr = Join-Path $artifactRoot 'sharedserver.stderr.log'
        host = '127.0.0.1'; port = 27224; native_port = 27223
    }
}

function Assert-SN2SharedFile {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path -Force
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'shared_unsafe_path: service file is a directory or symlink.'
        }
    }
}

function Read-SN2SharedManifest {
    param($Configuration)
    Assert-SN2SharedFile $Configuration.manifest
    if (-not (Test-Path -LiteralPath $Configuration.manifest)) { return $null }
    if ((Get-Item -LiteralPath $Configuration.manifest).Length -gt 65536) {
        throw 'shared_invalid_manifest: state file is too large.'
    }
    $record = Get-Content -LiteralPath $Configuration.manifest -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($name in @('schema_version', 'owner', 'repository_root', 'server_script', 'host', 'port', 'native_port', 'arguments', 'command_line', 'pid', 'creation_time', 'executable', 'launcher')) {
        if ($null -eq $record.PSObject.Properties[$name]) { throw 'shared_invalid_manifest: incomplete identity.' }
    }
    if ($record.creation_time -is [DateTime]) {
        $record.creation_time = $record.creation_time.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'", [Globalization.CultureInfo]::InvariantCulture)
    }
    if ($null -eq $record.launcher) { throw 'shared_invalid_manifest: launcher is missing.' }
    foreach ($name in @('pid', 'creation_time', 'executable', 'command_line')) {
        if ($null -eq $record.launcher.PSObject.Properties[$name]) { throw 'shared_invalid_manifest: incomplete launcher identity.' }
    }
    if ($record.launcher.creation_time -is [DateTime]) {
        $record.launcher.creation_time = $record.launcher.creation_time.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'", [Globalization.CultureInfo]::InvariantCulture)
    }
    if ($record.schema_version -ne 1 -or $record.owner -cne 'SN2_MCP_shared_server' -or
        $record.repository_root -ine $Configuration.root -or $record.server_script -ine $Configuration.script -or
        $record.executable -ine $Configuration.base_python -or $record.arguments -cne $Configuration.arguments -or
        $record.command_line -cne $Configuration.command_line -or
        $record.host -cne $Configuration.host -or $record.port -ne $Configuration.port -or
        $record.native_port -ne $Configuration.native_port -or
        ($record.pid -isnot [int] -and $record.pid -isnot [long]) -or $record.pid -lt 1 -or $record.pid -gt [uint32]::MaxValue -or
        $record.creation_time -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}Z$' -or
        $record.launcher.executable -ine $Configuration.python -or $record.launcher.command_line -cne $Configuration.command_line -or
        ($record.launcher.pid -isnot [int] -and $record.launcher.pid -isnot [long]) -or
        $record.launcher.pid -lt 1 -or $record.launcher.pid -gt [uint32]::MaxValue -or
        $record.launcher.creation_time -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}Z$' -or
        [string]::CompareOrdinal($record.creation_time, $record.launcher.creation_time) -lt 0 -or
        (($record.pid -eq $record.launcher.pid) -and ($record.creation_time -cne $record.launcher.creation_time -or
            $record.executable -ine $record.launcher.executable)) -or
        [string]::IsNullOrWhiteSpace($record.command_line)) {
        throw 'shared_invalid_manifest: record does not belong to this server.'
    }
    return $record
}

function Get-SN2SharedListeners {
    param($Configuration)
    @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalPort -eq $Configuration.port })
}

function Assert-SN2SharedPort {
    param($Configuration, $Listeners, [uint32]$ProcessId = 0)
    foreach ($listener in @($Listeners)) {
        if ($ProcessId -eq 0 -or $listener.OwningProcess -ne $ProcessId -or $listener.LocalAddress -cne $Configuration.host) {
            throw 'shared_port_conflict: port 27224 is occupied by another process or address.'
        }
    }
}

function Assert-SN2SharedNoUntrackedProcess {
    param($Configuration)
    foreach ($candidate in @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction Stop)) {
        if (($candidate.ExecutablePath -ieq $Configuration.python -or $candidate.ExecutablePath -ieq $Configuration.base_python) -and
            $candidate.CommandLine -ceq $Configuration.command_line) {
            throw 'shared_untracked_process: server is already running without a verifiable state record.'
        }
    }
}

function Open-SN2SharedProcess {
    param($Configuration, $Record, [switch]$ForTermination)
    $candidate = @(Get-CimInstance Win32_Process -Filter ('ProcessId=' + [uint32]$Record.pid) -ErrorAction Stop)
    if ($candidate.Count -eq 0) { return $null }
    if ($candidate.Count -ne 1 -or $candidate[0].ExecutablePath -ine $Record.executable -or
        $candidate[0].CommandLine -cne $Record.command_line) {
        throw 'shared_process_changed: PID or command line differs from the recorded values.'
    }
    $lease = Open-SN2CheckedProcess -ExpectedPid $Record.pid -ExpectedCreationTime $Record.creation_time `
        -ExpectedExecutable $Record.executable -ForTermination:$ForTermination
    try {
        $fresh = @(Get-CimInstance Win32_Process -Filter ('ProcessId=' + [uint32]$Record.pid) -ErrorAction Stop)
        if ($fresh.Count -ne 1 -or $fresh[0].CommandLine -cne $Record.command_line -or
            $fresh[0].ExecutablePath -ine $Record.executable) {
            throw 'shared_process_changed: process changed during verification.'
        }
        return $lease
    } catch { $lease.Dispose(); throw }
}

function Open-SN2SharedLauncher {
    param($Configuration, $Record, [switch]$ForTermination)
    Open-SN2SharedProcess $Configuration $Record.launcher -ForTermination:$ForTermination
}

function Find-SN2SharedChild {
    param($Configuration, $LauncherLease)
    if ($Configuration.base_python -ieq $Configuration.python) { return $LauncherLease }
    $parent = ConvertTo-SN2ProcessIdentity $LauncherLease.Identity
    $candidates = @(Get-CimInstance Win32_Process -Filter ('ParentProcessId=' + $parent.pid) -ErrorAction Stop | Where-Object {
        $_.ExecutablePath -ieq $Configuration.base_python -and $_.CommandLine -ceq $Configuration.command_line
    })
    if ($candidates.Count -gt 1) { throw 'shared_launch_children: launcher created multiple server processes.' }
    if ($candidates.Count -eq 0) { return $null }
    $lease = [SN2MCP.ProcessControl.ProcessLease]::Open([uint32]$candidates[0].ProcessId, $true)
    try {
        if ($lease.Identity.Executable -ine $Configuration.base_python -or
            [string]::CompareOrdinal($lease.Identity.CreationTime, $parent.creation_time) -lt 0 -or $LauncherLease.Wait(0)) {
            throw 'shared_launch_child_changed: child Python does not belong to the live launcher.'
        }
        $fresh = @(Get-CimInstance Win32_Process -Filter ('ProcessId=' + $lease.Identity.Pid) -ErrorAction Stop)
        $created = [DateTime]::ParseExact($lease.Identity.CreationTime, "yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'",
            [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal)
        if ($fresh.Count -ne 1 -or $fresh[0].ParentProcessId -ne $parent.pid -or
            $fresh[0].ExecutablePath -ine $Configuration.base_python -or $fresh[0].CommandLine -cne $Configuration.command_line -or
            $null -eq $fresh[0].CreationDate -or [Math]::Abs(($fresh[0].CreationDate.ToUniversalTime() - $created).TotalMilliseconds) -gt 1) {
            throw 'shared_launch_child_changed: child Python changed during verification.'
        }
        return $lease
    } catch { $lease.Dispose(); throw }
}

function Write-SN2SharedManifest {
    param($Configuration, $Record)
    Assert-SN2SharedFile $Configuration.manifest
    $temporary = Join-Path $Configuration.artifacts ('.sharedserver-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        $raw = [Text.UTF8Encoding]::new($false).GetBytes(($Record | ConvertTo-Json -Depth 5) + [Environment]::NewLine)
        $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($raw, 0, $raw.Length) } finally { $stream.Dispose() }
        Move-Item -LiteralPath $temporary -Destination $Configuration.manifest -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Invoke-SN2SharedStart {
    param($Configuration, [int]$TimeoutSeconds)
    foreach ($path in @($Configuration.python, $Configuration.script)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'shared_dependency_absent: Python or server code is missing.' }
    }
    if (-not (Test-Path -LiteralPath $Configuration.artifacts)) { $null = New-Item -ItemType Directory -Path $Configuration.artifacts }
    $record = Read-SN2SharedManifest $Configuration
    $lease = $null
    $launcherLease = $null
    try {
        if ($null -ne $record) {
            $launcherLease = Open-SN2SharedLauncher $Configuration $record
            $lease = Open-SN2SharedProcess $Configuration $record
            if ($null -ne $lease) {
                $listeners = @(Get-SN2SharedListeners $Configuration)
                Assert-SN2SharedPort $Configuration $listeners $record.pid
                if ($listeners.Count -eq 0) { throw 'shared_not_listening: recorded process is not yet listening on the port.' }
                return [pscustomobject]@{ state = 'already_running'; process = $record; url = 'http://127.0.0.1:27224/mcp' }
            }
            if ($null -ne $launcherLease) { throw 'shared_launcher_still_running: launcher remains without a server process.' }
        }
        Assert-SN2SharedPort $Configuration @(Get-SN2SharedListeners $Configuration)
        Assert-SN2SharedNoUntrackedProcess $Configuration
        foreach ($log in @($Configuration.stdout, $Configuration.stderr)) { Assert-SN2SharedFile $log }
        $child = Start-Process -FilePath $Configuration.python -ArgumentList $Configuration.arguments `
            -WorkingDirectory $Configuration.root -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $Configuration.stdout -RedirectStandardError $Configuration.stderr
        try {
            $created = $child.StartTime.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'", [Globalization.CultureInfo]::InvariantCulture)
            $launcherLease = Open-SN2CheckedProcess -ExpectedPid $child.Id -ExpectedCreationTime $created `
                -ExpectedExecutable $Configuration.python -ForTermination
            $launcherIdentity = ConvertTo-SN2ProcessIdentity $launcherLease.Identity
            $candidate = @(Get-CimInstance Win32_Process -Filter ('ProcessId=' + $child.Id) -ErrorAction Stop)
            if ($launcherIdentity.executable -ine $Configuration.python -or $candidate.Count -ne 1 -or
                $candidate[0].CommandLine -cne $Configuration.command_line) {
                throw 'shared_launch_identity: launched process does not match the command.'
            }
            $launcher = [pscustomobject]@{ pid = $launcherIdentity.pid; creation_time = $launcherIdentity.creation_time
                executable = $launcherIdentity.executable; command_line = $candidate[0].CommandLine }
            $childDeadline = [Diagnostics.Stopwatch]::StartNew()
            while ($null -eq $lease -and $childDeadline.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
                if ($launcherLease.Wait(0)) { throw 'shared_launch_exited: launcher exited during startup.' }
                $lease = Find-SN2SharedChild $Configuration $launcherLease
                if ($null -eq $lease) { Start-Sleep -Milliseconds 50 }
            }
            if ($null -eq $lease) { throw 'shared_child_timeout: child server did not appear.' }
            $identity = ConvertTo-SN2ProcessIdentity $lease.Identity
            $record = [pscustomobject]@{
                schema_version = 1; owner = 'SN2_MCP_shared_server'; repository_root = $Configuration.root
                server_script = $Configuration.script; host = $Configuration.host; port = $Configuration.port
                native_port = $Configuration.native_port; arguments = $Configuration.arguments
                command_line = $Configuration.command_line; pid = $identity.pid; launcher = $launcher
                creation_time = $identity.creation_time; executable = $identity.executable
            }
            Write-SN2SharedManifest $Configuration $record
            $deadline = [Diagnostics.Stopwatch]::StartNew()
            while ($deadline.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
                if ($lease.Wait(0)) { throw 'shared_launch_exited: server exited before opening the port; check stderr.' }
                $listeners = @(Get-SN2SharedListeners $Configuration)
                Assert-SN2SharedPort $Configuration $listeners $record.pid
                if ($listeners.Count -gt 0) {
                    return [pscustomobject]@{ state = 'started'; process = $record; url = 'http://127.0.0.1:27224/mcp' }
                }
                Start-Sleep -Milliseconds 100
            }
            throw 'shared_launch_timeout: server did not open the port within the allotted time.'
        } catch {
            $launchError = $_
            if ($null -ne $lease) {
                $null = Stop-SN2CheckedProcess -Lease $lease -GraceMilliseconds 0 -ForceWaitMilliseconds 5000 -AllowForce
                if (Test-Path -LiteralPath $Configuration.manifest) { Remove-Item -LiteralPath $Configuration.manifest -Force }
            }
            if ($null -ne $launcherLease -and $launcherLease -ne $lease) {
                $null = Stop-SN2CheckedProcess -Lease $launcherLease -GraceMilliseconds 0 -ForceWaitMilliseconds 5000 -AllowForce
            }
            throw $launchError
        } finally { $child.Dispose() }
    } finally {
        if ($null -ne $lease) { $lease.Dispose() }
        if ($null -ne $launcherLease -and $launcherLease -ne $lease) { $launcherLease.Dispose() }
    }
}

function Invoke-SN2SharedLocked {
    param([scriptblock]$Action)
    $mutex = [Threading.Mutex]::new($false, 'Local\SN2MCP.SharedServer.27224')
    $held = $false
    try {
        try { $held = $mutex.WaitOne(15000) } catch [Threading.AbandonedMutexException] { $held = $true }
        if (-not $held) { throw 'shared_operation_busy: another server operation is still running.' }
        & $Action
    } finally { if ($held) { $mutex.ReleaseMutex() }; $mutex.Dispose() }
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        $configuration = Get-SN2SharedConfiguration $RepositoryRoot
        $result = Invoke-SN2SharedLocked { Invoke-SN2SharedStart $configuration $StartupTimeoutSeconds }
        if ($Json) { $result | ConvertTo-Json -Depth 6 } else { $result }
    } catch {
        $systemFailure = $_.Exception.GetBaseException()
        if ($_.Exception.Message -cmatch '^shared_[a-z_]+: ') {
            [Console]::Error.WriteLine($_.Exception.Message)
        } elseif ($systemFailure.GetType().FullName -ceq 'SN2MCP.ProcessControl.ProcessControlException') {
            [Console]::Error.WriteLine($systemFailure.Message)
        } else {
            $message = 'The shared MCP server start operation failed (hresult={0})' -f $systemFailure.HResult
            if ($systemFailure -is [ComponentModel.Win32Exception]) {
                $message += ', winerror={0}' -f $systemFailure.NativeErrorCode
            }
            [Console]::Error.WriteLine($message)
        }
        exit 2
    }
}

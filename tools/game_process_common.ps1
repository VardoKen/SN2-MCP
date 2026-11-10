<#
.SYNOPSIS
Internal operations using a retained Win32 process HANDLE.
.DESCRIPTION
Importing this file only defines functions. The full path and exact FILETIME
are checked using the same HANDLE used to wait for the process and, when
AllowForce is explicit, terminate it. WM_CLOSE is a bounded graceful attempt.
Wrappers must separately restrict the allowed EXE to the selected game.
#>

Set-StrictMode -Version Latest

if ($null -eq ('G1RMCP.ProcessControl.ProcessLease' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace G1RMCP.ProcessControl {
    public sealed class ProcessControlException : Exception {
        public string Code { get; private set; }
        public bool OutcomeUnknown { get; private set; }
        public ProcessControlException(string code, string message, bool unknown)
            : base(message) { Code = code; OutcomeUnknown = unknown; }
    }

    public sealed class ProcessIdentity {
        public uint Pid { get; internal set; }
        public string CreationTime { get; internal set; }
        public string Executable { get; internal set; }
    }

    internal sealed class ProcessHandle : SafeHandleZeroOrMinusOneIsInvalid {
        internal ProcessHandle(IntPtr value) : base(true) { SetHandle(value); }
        protected override bool ReleaseHandle() { return Native.CloseHandle(handle); }
    }

    internal static class Native {
        [StructLayout(LayoutKind.Sequential)]
        internal struct FileTime { internal uint Low; internal uint High; }
        internal delegate bool EnumWindowsCallback(IntPtr window, IntPtr parameter);
        [DllImport("kernel32.dll", SetLastError = true)]
        internal static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
        [DllImport("kernel32.dll", SetLastError = true)]
        internal static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll", SetLastError = true)]
        internal static extern uint GetProcessId(ProcessHandle handle);
        [DllImport("kernel32.dll", SetLastError = true)]
        internal static extern bool GetProcessTimes(ProcessHandle handle, out FileTime created,
            out FileTime exited, out FileTime kernel, out FileTime user);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        internal static extern bool QueryFullProcessImageName(ProcessHandle handle, uint flags,
            StringBuilder name, ref uint length);
        [DllImport("kernel32.dll", SetLastError = true)]
        internal static extern uint WaitForSingleObject(ProcessHandle handle, uint milliseconds);
        [DllImport("kernel32.dll", SetLastError = true)]
        internal static extern bool TerminateProcess(ProcessHandle handle, uint exitCode);
        [DllImport("user32.dll", SetLastError = true)]
        internal static extern bool EnumWindows(EnumWindowsCallback callback, IntPtr parameter);
        [DllImport("user32.dll")]
        internal static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
        [DllImport("user32.dll")]
        internal static extern bool IsWindowVisible(IntPtr window);
        [DllImport("user32.dll", SetLastError = true)]
        internal static extern IntPtr SendMessageTimeout(IntPtr window, uint message, IntPtr wParam,
            IntPtr lParam, uint flags, uint timeout, out UIntPtr result);
    }

    public sealed class StopResult {
        public string State { get; internal set; }
        public string Mode { get; internal set; }
        public int GraceRequests { get; internal set; }
        public ProcessIdentity Identity { get; internal set; }
    }

    public sealed class ProcessLease : IDisposable {
        private const uint Query = 0x1000;
        private const uint Synchronize = 0x00100000;
        private const uint Terminate = 0x0001;
        private const uint WaitTimeout = 258;
        private ProcessHandle handle;
        private readonly bool canTerminate;
        public ProcessIdentity Identity { get; private set; }
        public bool IsDisposed { get { return handle == null || handle.IsClosed; } }

        private ProcessLease(ProcessHandle value, bool termination) {
            handle = value;
            canTerminate = termination;
            Identity = ReadIdentity(value);
        }

        private static ProcessControlException Failure(string code, string message, bool unknown) {
            return new ProcessControlException(code, message, unknown);
        }

        private static ProcessIdentity ReadIdentity(ProcessHandle value) {
            uint pid = Native.GetProcessId(value);
            Native.FileTime created, exited, kernel, user;
            if (pid == 0 || !Native.GetProcessTimes(value, out created, out exited, out kernel, out user))
                throw Failure("process_identity_unavailable", "Could not read process identity", false);
            ulong ticks = ((ulong)created.High << 32) | created.Low;
            if (ticks > Int64.MaxValue)
                throw Failure("process_identity_unavailable", "Invalid process creation time", false);
            var name = new StringBuilder(32768);
            uint length = (uint)name.Capacity;
            if (!Native.QueryFullProcessImageName(value, 0, name, ref length) || length == 0)
                throw Failure("process_identity_unavailable", "Could not read the full EXE path", false);
            return new ProcessIdentity {
                Pid = pid,
                CreationTime = DateTime.FromFileTimeUtc((long)ticks).ToString(
                    "yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'", CultureInfo.InvariantCulture),
                Executable = Path.GetFullPath(name.ToString())
            };
        }

        public static ProcessLease Open(uint pid, bool forTermination) {
            if (pid == 0) throw Failure("invalid_process_identity", "PID must be positive", false);
            IntPtr raw = Native.OpenProcess(Query | Synchronize | (forTermination ? Terminate : 0), false, pid);
            if (raw == IntPtr.Zero)
                throw Failure("process_unavailable", "Could not open the selected process", false);
            var safe = new ProcessHandle(raw);
            try { return new ProcessLease(safe, forTermination); }
            catch { safe.Dispose(); throw; }
        }

        public static ProcessLease OpenChecked(uint pid, string created, string executable, bool forTermination) {
            DateTime parsed;
            if (String.IsNullOrEmpty(created) || !DateTime.TryParseExact(created,
                "yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'", CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out parsed) ||
                String.IsNullOrEmpty(executable) || !Path.IsPathRooted(executable))
                throw Failure("invalid_process_identity", "Exact UTC time and an absolute EXE path are required", false);
            var lease = Open(pid, forTermination);
            try {
                if (lease.Identity.Pid != pid || lease.Identity.CreationTime != created ||
                    !String.Equals(lease.Identity.Executable, Path.GetFullPath(executable), StringComparison.OrdinalIgnoreCase))
                    throw Failure("process_changed", "PID, creation time, or full EXE path changed", false);
                return lease;
            }
            catch { lease.Dispose(); throw; }
        }

        private void AssertOpen() {
            if (IsDisposed) throw Failure("process_handle_closed", "Process HANDLE has already been released", false);
        }

        public bool Wait(int milliseconds) {
            AssertOpen();
            if (milliseconds < 0 || milliseconds > 25000)
                throw Failure("invalid_wait", "Wait duration is outside the allowed range", false);
            uint result = Native.WaitForSingleObject(handle, (uint)milliseconds);
            if (result == 0) return true;
            if (result == WaitTimeout) return false;
            throw Failure("process_wait_failed", "Could not verify termination of the original process", true);
        }

        public bool IsRunning() { return !Wait(0); }

        public string GetExitTime() {
            AssertOpen();
            if (!Wait(0))
                throw Failure("process_still_running", "Exit time is unavailable while the process is still running", false);
            Native.FileTime created, exited, kernel, user;
            if (!Native.GetProcessTimes(handle, out created, out exited, out kernel, out user))
                throw Failure("process_identity_unavailable", "Could not read the original process exit time", false);
            ulong ticks = ((ulong)exited.High << 32) | exited.Low;
            if (ticks == 0 || ticks > Int64.MaxValue)
                throw Failure("process_identity_unavailable", "Invalid original process exit time", false);
            return DateTime.FromFileTimeUtc((long)ticks).ToString(
                "yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'", CultureInfo.InvariantCulture);
        }

        private int RequestGracefulClose(Stopwatch elapsed, int budget) {
            var windows = new List<IntPtr>();
            Native.EnumWindowsCallback callback = delegate(IntPtr window, IntPtr parameter) {
                uint pid;
                Native.GetWindowThreadProcessId(window, out pid);
                if (pid == Identity.Pid && Native.IsWindowVisible(window)) windows.Add(window);
                return true;
            };
            Native.EnumWindows(callback, IntPtr.Zero);
            GC.KeepAlive(callback);
            int attempted = 0;
            foreach (IntPtr window in windows) {
                if (!IsRunning()) break;
                long remaining = budget - elapsed.ElapsedMilliseconds;
                if (remaining <= 0) break;
                uint owner;
                Native.GetWindowThreadProcessId(window, out owner);
                if (owner != Identity.Pid) continue;
                UIntPtr ignored;
                // The graceful message remains best effort: HWND is not a process HANDLE.
                Native.SendMessageTimeout(window, 0x0010, IntPtr.Zero, IntPtr.Zero,
                    0x0002 | 0x0020, (uint)Math.Min(1000L, remaining), out ignored);
                attempted++;
            }
            return attempted;
        }

        public StopResult Stop(int graceMilliseconds, int forceWaitMilliseconds, bool allowForce) {
            AssertOpen();
            if (graceMilliseconds < 0 || graceMilliseconds > 20000 ||
                forceWaitMilliseconds < 1 || forceWaitMilliseconds > 5000)
                throw Failure("invalid_wait", "Invalid process termination limit", false);
            if (allowForce && !canTerminate)
                throw Failure("termination_not_enabled", "HANDLE was opened without forced-termination access", false);
            if (!IsRunning())
                return new StopResult { State = "already_exited", Mode = "none", GraceRequests = 0, Identity = Identity };
            var elapsed = Stopwatch.StartNew();
            int attempts = RequestGracefulClose(elapsed, graceMilliseconds);
            int remaining = (int)Math.Max(0, graceMilliseconds - elapsed.ElapsedMilliseconds);
            if (Wait(remaining))
                return new StopResult { State = "stopped", Mode = "graceful", GraceRequests = attempts, Identity = Identity };
            if (!allowForce)
                throw Failure("process_still_running", "The original process did not exit gracefully", false);
            if (!Native.TerminateProcess(handle, 0)) {
                if (Wait(forceWaitMilliseconds))
                    return new StopResult { State = "stopped", Mode = "exited_before_force", GraceRequests = attempts, Identity = Identity };
                throw Failure("termination_failed", "Could not terminate the original process using the retained HANDLE", true);
            }
            if (!Wait(forceWaitMilliseconds))
                throw Failure("termination_outcome_unknown", "Termination of the original process was not confirmed", true);
            return new StopResult { State = "stopped", Mode = "forced", GraceRequests = attempts, Identity = Identity };
        }

        public void Dispose() {
            if (handle != null) { handle.Dispose(); handle = null; }
        }
    }
}
'@
}

function ConvertTo-G1RProcessIdentity {
    param([Parameter(Mandatory = $true)]$Identity)
    [pscustomobject]@{
        pid = [uint32]$Identity.Pid
        creation_time = [string]$Identity.CreationTime
        executable = [string]$Identity.Executable
    }
}

function Get-G1RProcessIdentity {
    param([Parameter(Mandatory = $true)][uint32]$ProcessId)
    $lease = [G1RMCP.ProcessControl.ProcessLease]::Open($ProcessId, $false)
    try { ConvertTo-G1RProcessIdentity -Identity $lease.Identity }
    finally { $lease.Dispose() }
}

function Open-G1RCheckedProcess {
    param(
        [Parameter(Mandatory = $true)][uint32]$ExpectedPid,
        [Parameter(Mandatory = $true)][string]$ExpectedCreationTime,
        [Parameter(Mandatory = $true)][string]$ExpectedExecutable,
        [switch]$ForTermination
    )
    [G1RMCP.ProcessControl.ProcessLease]::OpenChecked($ExpectedPid,
        $ExpectedCreationTime, $ExpectedExecutable, [bool]$ForTermination)
}

function Stop-G1RCheckedProcess {
    param(
        [Parameter(Mandatory = $true)]$Lease,
        [ValidateRange(0, 20000)][int]$GraceMilliseconds = 20000,
        [ValidateRange(1, 5000)][int]$ForceWaitMilliseconds = 5000,
        [switch]$AllowForce
    )
    $result = $Lease.Stop($GraceMilliseconds, $ForceWaitMilliseconds, [bool]$AllowForce)
    [pscustomobject]@{
        state = $result.State
        mode = $result.Mode
        grace_requests = $result.GraceRequests
        process = ConvertTo-G1RProcessIdentity -Identity $result.Identity
        outcome_unknown = $false
    }
}

function ConvertFrom-SN2NativeLoadResult {
    param([Parameter(Mandatory = $true)][string]$Source,
          [Parameter(Mandatory = $true)][uint32]$ExpectedPid)
    $nativeResult = $Source | ConvertFrom-Json -ErrorAction Stop
    if ($null -eq $nativeResult -or $null -eq $nativeResult.PSObject.Properties['ok'] -or
        $null -eq $nativeResult.PSObject.Properties['status'] -or $null -eq $nativeResult.PSObject.Properties['pid'] -or
        $nativeResult.ok -isnot [bool] -or $nativeResult.ok -ne $true -or $nativeResult.status -isnot [string] -or
        $nativeResult.status -notin @('loaded', 'already_loaded') -or
        ($nativeResult.pid -isnot [int] -and $nativeResult.pid -isnot [long]) -or $nativeResult.pid -ne $ExpectedPid) {
        throw 'The loader did not confirm LoadLibraryW for the original PID.'
    }
    $nativeResult
}

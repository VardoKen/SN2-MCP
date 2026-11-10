param(
    [string]$GameRoot = 'E:\Games\Subnautica 2',
    [string]$Executable,
    [switch]$Json
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
. (Join-Path $PSScriptRoot 'game_process_common.ps1')
try {
    $resolvedRoot = (Resolve-Path -LiteralPath $GameRoot -ErrorAction Stop).ProviderPath
    $allowed = if ([string]::IsNullOrWhiteSpace($Executable)) {
        @((Join-Path $resolvedRoot 'Subnautica2.exe'), (Join-Path $resolvedRoot 'Subnautica2\Binaries\Win64\Subnautica2-Win64-Shipping.exe'))
    } else {
        @((Resolve-Path -LiteralPath $Executable -ErrorAction Stop).ProviderPath)
    }
    $processes = @()
    foreach ($candidate in @(Get-Process -Name 'Subnautica2', 'Subnautica2-Win64-Shipping' -ErrorAction SilentlyContinue)) {
        $identity = Get-SN2ProcessIdentity -ProcessId $candidate.Id
        if ($allowed -contains $identity.executable) {
            $processes += $identity
        }
    }
    $result = [pscustomobject]@{ processes = @($processes); source = 'native_process_handle_snapshot' }
    if ($Json) { $result | ConvertTo-Json -Depth 5 -Compress } else { $result }
} catch {
    if ($Json) {
        [pscustomobject]@{ state = 'failed'; message = 'Could not unambiguously verify processes of the selected game.'; outcome_unknown = $false } | ConvertTo-Json -Compress
        exit 2
    }
    throw
}

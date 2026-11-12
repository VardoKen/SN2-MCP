[CmdletBinding()]
param(
    [ValidateSet('Release','Debug')][string]$Configuration = 'Release',
    [string]$Generator = 'Visual Studio 17 2022',
    [string]$Target = 'all',
    [int]$Parallel = 4,
    [double]$TimeoutSeconds = 900,
    [switch]$SkipTests,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
$taskRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) {
    $taskPython = (Get-Command python -ErrorAction Stop).Source
}
# Arguments are passed as tokens to the fixed module, without shell interpolation.
& $taskPython (Join-Path $taskRoot 'src\sn2_mcp\build_workflow.py') `
    --repository-root $taskRoot --configuration $Configuration --generator $Generator `
    --target $Target --parallel $Parallel --timeout-seconds $TimeoutSeconds `
    --run-tests ([string](-not $SkipTests.IsPresent)).ToLowerInvariant() --json ([string]$Json.IsPresent).ToLowerInvariant()
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

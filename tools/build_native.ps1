[CmdletBinding()]
param(
    [ValidateSet('Release','Debug')][string]$Configuration = 'Release',
    [string]$Generator = 'Visual Studio 17 2022'
)

$ErrorActionPreference = 'Stop'
$taskRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskSource = Join-Path $taskRoot 'native'
$taskBuild = Join-Path $taskRoot 'build\native'
if (-not (Test-Path -LiteralPath (Join-Path $taskSource 'CMakeLists.txt') -PathType Leaf)) {
    throw 'Native subproject CMakeLists.txt was not found'
}
Get-Command cmake -ErrorAction Stop | Out-Null
Get-Command ctest -ErrorAction Stop | Out-Null
& cmake -S $taskSource -B $taskBuild -G $Generator -A x64
if ($LASTEXITCODE -ne 0) { throw 'Native build configuration failed' }
& cmake --build $taskBuild --config $Configuration --parallel 4
if ($LASTEXITCODE -ne 0) { throw 'Native build failed' }
& ctest --test-dir $taskBuild -C $Configuration --output-on-failure
if ($LASTEXITCODE -ne 0) { throw 'Native checks failed' }
Write-Output ('Build and checks complete: ' + $Configuration)

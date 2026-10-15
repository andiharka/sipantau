param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$ReportPath)
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'ReleaseSecurity.psm1') -Force
$result = Test-ReleasePath -Path $Path -ReportPath $ReportPath
$result | ConvertTo-Json -Depth 8 | Write-Host

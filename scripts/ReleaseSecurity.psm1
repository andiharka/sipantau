Set-StrictMode -Version Latest

function Get-DefenderInfo {
    # Query the documented native CIM provider directly. Importing Defender in pwsh
    # otherwise creates a WinPS compatibility session on hosted Windows runners.
    return Get-CimInstance -Namespace 'root/Microsoft/Windows/Defender' -ClassName 'MSFT_MpComputerStatus' -ErrorAction Stop
}

function Get-DefenderSnapshot($Status) {
    $snapshot = [ordered]@{}
    foreach ($name in @('AMServiceEnabled', 'AntivirusEnabled', 'RealTimeProtectionEnabled', 'AMRunningMode',
                         'AntivirusSignatureVersion', 'AMEngineVersion')) {
        $property = $Status.PSObject.Properties[$name]
        $snapshot[$name] = if ($null -ne $property) { $property.Value } else { $null }
    }
    return $snapshot
}

function Find-DefenderCli {
    $platform = Join-Path $env:ProgramData 'Microsoft\Windows Defender\Platform'
    if (Test-Path -LiteralPath $platform) {
        $candidates = Get-ChildItem -LiteralPath $platform -Directory | Sort-Object Name -Descending
        foreach ($directory in $candidates) {
            $candidate = Join-Path $directory.FullName 'MpCmdRun.exe'
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        }
    }
    $fallback = Join-Path $env:ProgramFiles 'Windows Defender\MpCmdRun.exe'
    if (Test-Path -LiteralPath $fallback -PathType Leaf) { return $fallback }
    throw 'Microsoft Defender scanner is unavailable; publishing is blocked.'
}

function Invoke-DefenderCommand($Cli, $Arguments, $LogPath) {
    $previousPreference = $ErrorActionPreference
    try {
        # Native stderr may contain progress. Assess the exit code separately.
        $ErrorActionPreference = 'Continue'
        $global:LASTEXITCODE = 0
        & $Cli @Arguments *> $LogPath
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    if (Test-Path -LiteralPath $LogPath) { Get-Content -LiteralPath $LogPath | Write-Host }
    return [int]$code
}

function Get-Sha256($Path) {
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    $stream = $null
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        return [System.BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    } finally {
        if ($stream) { $stream.Dispose() }
        $hasher.Dispose()
    }
}

function Get-ArtifactInventory($Path) {
    if (![System.IO.File]::Exists($Path) -and ![System.IO.Directory]::Exists($Path)) {
        throw 'Release artifact is missing, possibly quarantined; publishing is blocked.'
    }
    $isDirectory = [System.IO.Directory]::Exists($Path)
    $files = if ($isDirectory) {
        [System.IO.Directory]::GetFiles($Path, '*', [System.IO.SearchOption]::AllDirectories)
    } else { @($Path) }
    $rows = @()
    foreach ($file in ($files | Sort-Object)) {
        $name = if ($isDirectory) { $file.Substring($Path.TrimEnd('\', '/').Length + 1).Replace('\', '/') }
                else { [System.IO.Path]::GetFileName($file) }
        $rows += [ordered]@{ path = $name; size = ([System.IO.FileInfo]$file).Length; sha256 = Get-Sha256 $file }
    }
    if ($rows.Count -eq 0) { throw 'Release artifact is empty; publishing is blocked.' }
    return $rows
}

function Test-ReleasePath {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$ReportPath)
    $ErrorActionPreference = 'Stop'
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $reportFile = [System.IO.Path]::GetFullPath($ReportPath)
    [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($reportFile)) | Out-Null
    $report = [ordered]@{ outcome = 'blocked'; started_at = [DateTime]::UtcNow.ToString('o');
        finished_at = $null; path = $fullPath; signature_version = $null; engine_version = $null;
        scan_policy = 'on-demand'; defender_before = $null; defender_after_update = $null;
        scan_exit_code = $null; error = $null; files = @() }
    try {
        $before = @(Get-ArtifactInventory $fullPath)
        $report.files = $before
        $status = Get-DefenderInfo
        $report.defender_before = Get-DefenderSnapshot $status
        # Hosted runner images disable real-time monitoring. This is separate from
        # the custom scanner's ability to examine every file and report detections.
        if ($status.RealTimeProtectionEnabled -ne $true) {
            Write-Warning 'Real-time protection is off on this runner. Publication still requires a successful on-demand scan.'
        }
        $cli = Find-DefenderCli
        # Assess service and engine readiness after the native signature update.
        $updateCode = Invoke-DefenderCommand $cli @('-SignatureUpdate') ($reportFile + '.signature.log')
        if ($updateCode -ne 0) { throw "Defender security intelligence update failed (exit $updateCode)." }
        $status = Get-DefenderInfo
        $report.defender_after_update = Get-DefenderSnapshot $status
        $report.signature_version = $status.AntivirusSignatureVersion
        $report.engine_version = $status.AMEngineVersion
        if ($status.AMServiceEnabled -ne $true) {
            throw 'Microsoft Defender service is not running after signature update; an on-demand scan cannot be verified. See defender_after_update in the scan report.'
        }
        if ([string]::IsNullOrWhiteSpace($status.AntivirusSignatureVersion) -or $status.AntivirusSignatureVersion -eq '0.0.0.0') {
            throw 'Defender security intelligence version is unavailable; publishing is blocked.'
        }
        if ([string]::IsNullOrWhiteSpace($status.AMEngineVersion) -or $status.AMEngineVersion -eq '0.0.0.0') {
            throw 'Defender scan engine version is unavailable; publishing is blocked.'
        }
        # DisableRemediation applies ONLY to this custom scan: inspect without modifying the artifact.
        # This does not alter real-time protection settings. Any detection/error blocks publication.
        $code = Invoke-DefenderCommand $cli @('-Scan', '-ScanType', '3', '-File', $fullPath, '-DisableRemediation') ($reportFile + '.scan.log')
        $report.scan_exit_code = $code
        if ($code -ne 0) { throw "Defender detected a threat or could not finish the scan (exit $code)." }
        $after = @(Get-ArtifactInventory $fullPath)
        $beforeJson = ConvertTo-Json -InputObject $before -Depth 5 -Compress
        $afterJson = ConvertTo-Json -InputObject $after -Depth 5 -Compress
        if ($beforeJson -cne $afterJson) {
            throw 'Artifact changed or files disappeared during scanning; publishing is blocked.'
        }
        $report.files = $after
        $report.outcome = 'passed'
    } catch {
        $report.error = $_.Exception.Message
        throw
    } finally {
        $report.finished_at = [DateTime]::UtcNow.ToString('o')
        $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $reportFile -Encoding UTF8
    }
    return $report
}

Export-ModuleMember -Function Test-ReleasePath

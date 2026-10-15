"""Opt-in Windows executable updates from the project's public GitHub releases.

No Pantau credentials are sent to GitHub. Config, cached policy and state are never replaced.
"""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid
import requests

REPOSITORY = 'andiharka/sipantau'
MAX_DOWNLOAD = 128 * 1024 * 1024


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?\d+\.\d+\.\d+', value):
        raise ValueError('Expected stable vMAJOR.MINOR.PATCH release')
    return tuple(int(n) for n in value.lstrip('v').split('.'))


def select_asset(release, current):
    if release.get('draft') or release.get('prerelease'):
        return None
    tag = release.get('tag_name')
    if version_tuple(tag) <= version_tuple(current):
        return None
    assets = [a for a in release.get('assets', []) if a.get('name') == 'SIPANTAU.exe']
    if len(assets) != 1:
        raise ValueError('Release must contain exactly one SIPANTAU.exe')
    asset = assets[0]
    digest = asset.get('digest', '')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('Release executable has no SHA-256 digest')
    expected_url = f'https://github.com/{REPOSITORY}/releases/download/{tag}/SIPANTAU.exe'
    if asset.get('browser_download_url') != expected_url:
        raise ValueError('Unexpected release asset URL')
    size = asset.get('size')
    if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= MAX_DOWNLOAD:
        raise ValueError('Invalid release asset size')
    return tag, asset


def fetch_update(session, current_version):
    response = session.get(f'https://api.github.com/repos/{REPOSITORY}/releases/latest',
                           timeout=(5, 15), verify=True)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get('draft') or data.get('prerelease'):
        raise ValueError('Expected public stable release metadata')
    return select_asset(data, current_version)


def check_for_update(current_version):
    """Read and validate public release metadata independently of automatic-update opt-in."""
    with requests.Session() as session:
        session.headers.update({'Accept': 'application/vnd.github+json', 'User-Agent': 'SIPANTAU-Updater'})
        return fetch_update(session, current_version)


def download_asset(session, asset, destination):
    destination = Path(destination)
    part = destination.with_suffix('.part')
    try:
        with session.get(asset['browser_download_url'], stream=True, timeout=(5, 30), verify=True) as response:
            response.raise_for_status()
            digest = hashlib.sha256()
            size = 0
            with part.open('wb') as output:
                for chunk in response.iter_content(128 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > asset['size'] or size > MAX_DOWNLOAD:
                        raise ValueError('Download exceeded declared size')
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        if size != asset['size'] or digest.hexdigest() != asset['digest'].split(':', 1)[1]:
            raise ValueError('Download size or SHA-256 mismatch')
        with part.open('rb') as binary:
            if binary.read(2) != b'MZ':
                raise ValueError('Asset is not a Windows executable')
        os.replace(part, destination)
    finally:
        part.unlink(missing_ok=True)


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def installer_script(executable, staged, marker, parent_pid, digest, tag):
    # PowerShell literals quote paths; release text is never executed as shell code.
    target = Path(executable)
    backup = target.with_suffix('.previous.exe')
    status = target.parent / 'update-status.json'
    return fr'''$ErrorActionPreference = 'Stop'
$target = {ps_quote(target)}
$staged = {ps_quote(staged)}
$backup = {ps_quote(backup)}
$marker = {ps_quote(marker)}
$status = {ps_quote(status)}
$agentProcess = $null
$replaced = $false
$phase = 'wait-parent'
function Save-Outcome($outcome, $detail = $null) {{
    $agentId = $null
    if ($agentProcess) {{ $agentId = $agentProcess.Id }}
    @{{ version = {ps_quote(tag)}; outcome = $outcome; phase = $phase; error = $detail;
        agent_pid = $agentId; time = (Get-Date).ToUniversalTime().ToString('o') }} |
        ConvertTo-Json | Set-Content -LiteralPath $status -Encoding UTF8
}}
function Start-Agent($healthCheck) {{
    $start = @{{ FilePath = $target; WorkingDirectory = {ps_quote(target.parent)}; PassThru = $true }}
    if ($healthCheck) {{ $start.ArgumentList = '--update-health "' + $marker + '"' }}
    return Start-Process @start
}}
function Stop-AgentTree($process) {{
    if (!$process) {{ return }}
    $process.Refresh()
    if ($process.HasExited) {{ return }}
    # PyInstaller has a bootloader parent and a Python child; stop both before restoring the image.
    $previousPreference = $ErrorActionPreference
    try {{
        $ErrorActionPreference = 'Continue'
        & "$env:SystemRoot\System32\taskkill.exe" /PID $process.Id /T /F 2>&1 | Out-Null
    }} finally {{ $ErrorActionPreference = $previousPreference }}
    if (!$process.WaitForExit(10000)) {{ throw 'Updated agent process did not stop' }}
}}
try {{
    $parentProcess = $null
    try {{ $parentProcess = [System.Diagnostics.Process]::GetProcessById({int(parent_pid)}) }}
    catch [System.ArgumentException] {{ }} # The parent may already have exited.
    if ($parentProcess) {{
        try {{
            if (!$parentProcess.WaitForExit(60000)) {{ throw 'Previous agent did not exit within 60 seconds' }}
        }} catch [System.InvalidOperationException] {{ }}
        finally {{ $parentProcess.Dispose() }}
    }}
    $phase = 'verify-download'
    # Use .NET directly: Get-FileHash may be unavailable in an inherited PowerShell environment.
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    $hashStream = $null
    try {{
        $hashStream = [System.IO.File]::OpenRead($staged)
        $stagedHash = [System.BitConverter]::ToString($hasher.ComputeHash($hashStream)).Replace('-', '').ToLowerInvariant()
    }} finally {{
        # Release the executable handle before replacement, even if hashing fails.
        if ($hashStream) {{ $hashStream.Dispose() }}
        $hasher.Dispose()
    }}
    if ($stagedHash -ne {ps_quote(digest)}) {{
        throw 'Staged executable failed verification'
    }}
    $phase = 'replace-executable'
    $replaceDeadline = (Get-Date).AddSeconds(30)
    while ($true) {{
        try {{
            # One operation replaces the target and saves its previous bytes in the backup.
            [System.IO.File]::Replace($staged, $target, $backup)
            $replaced = $true
            break
        }} catch [System.IO.IOException] {{
            if ((Get-Date) -ge $replaceDeadline) {{ throw }}
            Start-Sleep -Milliseconds 250
        }} catch [System.UnauthorizedAccessException] {{
            if ((Get-Date) -ge $replaceDeadline) {{ throw }}
            Start-Sleep -Milliseconds 250
        }}
    }}
    $phase = 'start-updated-agent'
    Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue
    $agentProcess = Start-Agent $true
    $phase = 'wait-readiness'
    $deadline = (Get-Date).AddSeconds(180)
    while ((Get-Date) -lt $deadline) {{
        $agentProcess.Refresh()
        if ($agentProcess.HasExited) {{ throw 'Updated agent exited before readiness' }}
        if (Test-Path -LiteralPath $marker) {{ break }}
        Start-Sleep -Milliseconds 250
    }}
    if (!(Test-Path -LiteralPath $marker)) {{ throw 'Updated agent readiness timed out' }}
    Remove-Item -LiteralPath $marker -Force
    Save-Outcome 'installed'
}} catch {{
    $failure = $_.Exception.Message
    $failedPhase = $phase
    if ($replaced) {{
        try {{
            $phase = 'stop-updated-agent'
            Stop-AgentTree $agentProcess
            $phase = 'restore-backup'
            $restoreDeadline = (Get-Date).AddSeconds(30)
            while ($true) {{
                try {{ [System.IO.File]::Copy($backup, $target, $true); break }}
                catch [System.IO.IOException] {{
                    if ((Get-Date) -ge $restoreDeadline) {{ throw }}
                    Start-Sleep -Milliseconds 250
                }} catch [System.UnauthorizedAccessException] {{
                    if ((Get-Date) -ge $restoreDeadline) {{ throw }}
                    Start-Sleep -Milliseconds 250
                }}
            }}
            $phase = 'restart-previous-agent'
            $agentProcess = Start-Agent $false
            $phase = $failedPhase
            Save-Outcome 'rolled-back' $failure
        }} catch {{
            Save-Outcome 'rollback-failed' ($failure + '; recovery: ' + $_.Exception.Message)
        }}
    }} else {{
        # The original target is intact; restarting must also be observed by the test harness.
        try {{ $agentProcess = Start-Agent $false }}
        catch {{ $failure += '; restart: ' + $_.Exception.Message }}
        $phase = $failedPhase
        Save-Outcome 'failed' $failure
    }}
}}
'''


def signal_ready(config_dir, locked, healthy):
    if '--update-health' not in sys.argv or not healthy:
        return
    index = sys.argv.index('--update-health') + 1
    if index >= len(sys.argv):
        return
    marker = Path(sys.argv[index])
    if marker.parent.resolve() != Path(config_dir).resolve() or not re.fullmatch(r'update-health-[0-9a-f]{32}\.ready', marker.name):
        return
    marker.write_text('ready', encoding='ascii')


class ReleaseUpdater:
    def __init__(self, config, config_dir, current_version, enqueue, can_install, install):
        self.enabled = config.get('auto_update', False) is True and os.name == 'nt' and getattr(sys, 'frozen', False)
        self.directory = Path(config_dir)
        self.current_version = current_version
        self.enqueue = enqueue
        self.can_install = can_install
        self.install = install
        self.stop = threading.Event()
        self.in_progress = False

    def check_once(self):
        if not self.enabled or self.in_progress or not self.can_install():
            return
        with requests.Session() as session:
            session.headers.update({'Accept': 'application/vnd.github+json', 'User-Agent': 'SIPANTAU-Updater'})
            selected = fetch_update(session, self.current_version)
            if selected is None:
                return
            tag, asset = selected
            status_path = self.directory / 'update-status.json'
            if status_path.exists():
                try:
                    status = json.loads(status_path.read_text(encoding='utf-8-sig'))
                    if status.get('version') == tag and status.get('outcome') in ('failed', 'rolled-back', 'rollback-failed'):
                        return
                except (ValueError, OSError):
                    pass
            staged = self.directory / 'SIPANTAU.update.exe'
            download_asset(session, asset, staged)
        self.in_progress = True
        def apply():
            try:
                # Recheck on the GUI thread: a session may have started during download.
                if self.can_install():
                    self.install(staged, tag, asset['digest'].split(':', 1)[1])
            except Exception as error:
                logging.error('Update installation postponed (%s).', type(error).__name__)
            finally:
                self.in_progress = False
        self.enqueue(apply)

    def run(self):
        if not self.enabled:
            return
        self.stop.wait(30)
        while not self.stop.is_set():
            try:
                self.check_once()
            except Exception as error:
                logging.warning('Update check deferred (%s); current agent continues offline.', type(error).__name__)
            self.stop.wait(6 * 3600)

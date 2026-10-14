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
    # All interpolated paths are quoted as PowerShell literals. No shell text from a release is run.
    target = Path(executable)
    backup = target.with_suffix('.previous.exe')
    status = target.parent / 'update-status.json'
    return f'''$ErrorActionPreference = 'Stop'
$target = {ps_quote(target)}
$staged = {ps_quote(staged)}
$backup = {ps_quote(backup)}
$marker = {ps_quote(marker)}
$status = {ps_quote(status)}
$child = $null
$replaced = $false
function Save-Outcome($outcome) {{
    @{{ version = {ps_quote(tag)}; outcome = $outcome; time = (Get-Date).ToUniversalTime().ToString('o') }} |
        ConvertTo-Json | Set-Content -LiteralPath $status -Encoding UTF8
}}
try {{
    $parent = Get-Process -Id {int(parent_pid)} -ErrorAction SilentlyContinue
    if ($parent) {{ Wait-Process -Id {int(parent_pid)} -Timeout 60 }}
    if ((Get-FileHash -LiteralPath $staged -Algorithm SHA256).Hash.ToLower() -ne {ps_quote(digest)}) {{
        throw 'Staged executable failed verification'
    }}
    Copy-Item -LiteralPath $target -Destination $backup -Force
    # PyInstaller's parent bootloader may release the executable shortly after its child exits.
    $replaceDeadline = (Get-Date).AddSeconds(30)
    while ($true) {{
        try {{ Move-Item -LiteralPath $staged -Destination $target -Force; break }}
        catch {{
            if ((Get-Date) -ge $replaceDeadline) {{ throw }}
            Start-Sleep -Milliseconds 250
        }}
    }}
    $replaced = $true
    Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue
    $arguments = '--update-health "' + $marker + '"'
    $child = Start-Process -FilePath $target -ArgumentList $arguments -WorkingDirectory {ps_quote(target.parent)} -PassThru
    $deadline = (Get-Date).AddSeconds(180)
    while ((Get-Date) -lt $deadline -and !(Test-Path -LiteralPath $marker)) {{
        if ($child.HasExited) {{ throw 'Updated agent exited before readiness' }}
        Start-Sleep -Seconds 1
        $child.Refresh()
    }}
    if (!(Test-Path -LiteralPath $marker)) {{ throw 'Updated agent readiness timed out' }}
    Remove-Item -LiteralPath $marker -Force
    Save-Outcome 'installed'
}} catch {{
    if ($replaced) {{
        if ($child -and !$child.HasExited) {{ Stop-Process -Id $child.Id -Force; $child.WaitForExit() }}
        Copy-Item -LiteralPath $backup -Destination $target -Force
        Start-Process -FilePath $target -WorkingDirectory {ps_quote(target.parent)}
        Save-Outcome 'rolled-back'
    }} else {{
        Save-Outcome 'failed'
        Start-Process -FilePath $target -WorkingDirectory {ps_quote(target.parent)}
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
            response = session.get(f'https://api.github.com/repos/{REPOSITORY}/releases/latest', timeout=(5, 15), verify=True)
            response.raise_for_status()
            selected = select_asset(response.json(), self.current_version)
            if selected is None:
                return
            tag, asset = selected
            status_path = self.directory / 'update-status.json'
            if status_path.exists():
                try:
                    status = json.loads(status_path.read_text(encoding='utf-8-sig'))
                    if status.get('version') == tag and status.get('outcome') in ('failed', 'rolled-back'):
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

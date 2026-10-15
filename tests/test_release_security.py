"""Test release-gate decisions with mocked Defender commands; no malware sample is run."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

POWERSHELL = shutil.which('pwsh') or shutil.which('powershell.exe')
MODULE = Path(__file__).resolve().parent.parent/'scripts'/'ReleaseSecurity.psm1'


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(POWERSHELL, 'Requires PowerShell to exercise the Defender release gate')
class ReleaseSecurityTests(unittest.TestCase):
    def test_gate_passes_clean_files_and_blocks_detection_quarantine_disabled_or_unavailable_scanner(self):
        for case in ('clean', 'detected', 'quarantined', 'modified', 'disabled', 'unavailable', 'update-failed'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root/'package with spaces'
                target.mkdir()
                (target/'SIPANTAU.exe').write_bytes(b'MZtest executable')
                (target/'runtime.dll').write_bytes(b'MZtest runtime')
                report = root/'report.json'
                script = root/'run.ps1'
                body = f'''$ErrorActionPreference = 'Stop'
Import-Module {quote(MODULE)} -Force
$module = Get-Module ReleaseSecurity
& $module {{
    $script:TestCase = {quote(case)}
    function script:Get-DefenderInfo {{
        return [pscustomobject]@{{ AntivirusEnabled = ($script:TestCase -ne 'disabled');
            AMServiceEnabled = $true; RealTimeProtectionEnabled = $true;
            AntivirusSignatureVersion = 'test-signatures'; AMEngineVersion = 'test-engine' }}
    }}
    function script:Find-DefenderCli {{
        if ($script:TestCase -eq 'unavailable') {{ throw 'Scanner unavailable' }}
        return 'mock-scanner'
    }}
    function script:Invoke-DefenderCommand($Cli, $Arguments, $LogPath) {{
        if ($Arguments[0] -eq '-SignatureUpdate') {{
            if ($script:TestCase -eq 'update-failed') {{ return 2 }}
            return 0
        }}
        if ($Arguments[0] -eq '-Scan') {{
            if ($script:TestCase -eq 'detected') {{ return 2 }}
            if ($script:TestCase -eq 'quarantined') {{
                [System.IO.File]::Delete([System.IO.Path]::Combine($Arguments[4], 'SIPANTAU.exe'))
            }}
            if ($script:TestCase -eq 'modified') {{
                [System.IO.File]::WriteAllText([System.IO.Path]::Combine($Arguments[4], 'SIPANTAU.exe'), 'changed bytes')
            }}
            return 0
        }}
        throw 'Unexpected scanner invocation'
    }}
}}
try {{ Test-ReleasePath -Path {quote(target)} -ReportPath {quote(report)} | Out-Null }}
catch {{ Write-Host $_.Exception.Message; exit 1 }}
'''
                script.write_text(body, encoding='utf-8-sig')
                result = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-File', str(script)],
                                        capture_output=True, text=True, timeout=30)
                self.assertTrue(report.exists(), result.stdout + result.stderr)
                data = json.loads(report.read_text(encoding='utf-8-sig'))
                self.assertEqual(result.returncode, 0 if case == 'clean' else 1, result.stdout + result.stderr)
                self.assertEqual(data['outcome'], 'passed' if case == 'clean' else 'blocked', data)
                if case == 'clean':
                    self.assertEqual(len(data['files']), 2)
                    self.assertEqual(data['signature_version'], 'test-signatures')
                else:
                    self.assertTrue(data['error'], data)

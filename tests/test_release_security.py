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
    def test_gate_requires_successful_on_demand_scan_including_when_real_time_protection_is_off(self):
        for case in ('clean', 'on-demand-only', 'passive', 'service-initialized', 'detected', 'on-demand-detected', 'quarantined', 'modified',
                     'service-disabled', 'unavailable', 'update-failed', 'engine-missing', 'signatures-missing'):
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
    $script:StatusCalls = 0
    function script:Get-DefenderInfo {{
        $script:StatusCalls += 1
        return [pscustomobject]@{{ AntivirusEnabled = ($script:TestCase -ne 'passive');
            AMServiceEnabled = ($script:TestCase -ne 'service-disabled' -and ($script:TestCase -ne 'service-initialized' -or $script:StatusCalls -gt 1));
            RealTimeProtectionEnabled = ($script:TestCase -notin @('on-demand-only', 'passive', 'on-demand-detected'));
            AMRunningMode = $(if ($script:TestCase -eq 'passive') {{ 'Passive' }} else {{ 'Normal' }});
            AntivirusSignatureVersion = $(if ($script:TestCase -eq 'signatures-missing') {{ '' }} else {{ 'test-signatures' }});
            AMEngineVersion = $(if ($script:TestCase -eq 'engine-missing') {{ '0.0.0.0' }} else {{ 'test-engine' }}) }}
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
            if ($script:TestCase -in @('detected', 'on-demand-detected')) {{ return 2 }}
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
                passed = case in ('clean', 'on-demand-only', 'passive', 'service-initialized')
                self.assertEqual(result.returncode, 0 if passed else 1, result.stdout + result.stderr)
                self.assertEqual(data['outcome'], 'passed' if passed else 'blocked', data)
                self.assertEqual(data['scan_policy'], 'on-demand')
                if passed:
                    self.assertEqual(len(data['files']), 2)
                    self.assertEqual(data['signature_version'], 'test-signatures')
                    self.assertEqual(data['defender_before']['RealTimeProtectionEnabled'], case in ('clean', 'service-initialized'))
                    self.assertTrue(data['defender_after_update']['AMServiceEnabled'])
                    self.assertEqual(data['scan_exit_code'], 0)
                else:
                    self.assertTrue(data['error'], data)

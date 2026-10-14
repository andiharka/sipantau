"""Exercise the actual replacement/rollback PowerShell helper in Windows CI.

Dummy executables keep this test independent of Tk, real locks and installed agents.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from updater import installer_script

CSC = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'


@unittest.skipUnless(os.name == 'nt' and CSC.exists(), 'Requires Windows .NET compiler and PowerShell')
class WindowsInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name)
        cls.binaries = {}
        programs = {
            'old': 'System.IO.File.WriteAllText(System.IO.Path.Combine(System.AppDomain.CurrentDomain.BaseDirectory, "rollback-started"), "yes");',
            'ready': 'if (args.Length == 2 && args[0] == "--update-health") System.IO.File.WriteAllText(args[1], "ready"); System.Threading.Thread.Sleep(3000);',
            'bad': 'System.Environment.Exit(1);',
        }
        for name, body in programs.items():
            source = cls.directory / (name + '.cs')
            binary = cls.directory / (name + '.exe')
            source.write_text('class Program { static void Main(string[] args) {' + body + '} }')
            subprocess.run([str(CSC), '/nologo', '/target:exe', '/out:' + str(binary), str(source)], check=True, capture_output=True)
            cls.binaries[name] = binary.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_installer(self, kind):
        directory = self.directory / kind
        directory.mkdir()
        target = directory / 'SIPANTAU.exe'
        target.write_bytes(self.binaries['old'])
        staged = directory / 'SIPANTAU.update.exe'
        staged.write_bytes(self.binaries[kind])
        marker = directory / ('update-health-' + 'b'*32 + '.ready')
        script = directory / 'install.ps1'
        script.write_text(installer_script(target, staged, marker, 2147483647,
                                          hashlib.sha256(staged.read_bytes()).hexdigest(), 'v1.2.0'), encoding='utf-8-sig')
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                        '-File', str(script)], check=True, capture_output=True, timeout=30)
        return directory, json.loads((directory/'update-status.json').read_text(encoding='utf-8-sig'))

    def test_actual_windows_replace_and_readiness(self):
        directory, status = self.run_installer('ready')
        self.assertEqual(status['outcome'], 'installed')
        self.assertEqual((directory/'SIPANTAU.exe').read_bytes(), self.binaries['ready'])
        self.assertEqual((directory/'SIPANTAU.previous.exe').read_bytes(), self.binaries['old'])
        # Wait until the test executable releases its image before temporary directory cleanup.
        time.sleep(4)

    def test_actual_windows_failed_startup_restores_old_executable(self):
        directory, status = self.run_installer('bad')
        self.assertEqual(status['outcome'], 'rolled-back')
        self.assertEqual((directory/'SIPANTAU.exe').read_bytes(), self.binaries['old'])
        for _ in range(20):
            if (directory/'rollback-started').exists(): break
            time.sleep(0.1)
        self.assertTrue((directory/'rollback-started').exists())
        time.sleep(0.5)

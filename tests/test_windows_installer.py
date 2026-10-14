"""Exercise actual replacement/rollback with harmless Windows dummy executables."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import psutil
from updater import installer_script

CSC = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'


@unittest.skipUnless(os.name == 'nt' and CSC.exists(), 'Requires Windows .NET compiler and PowerShell')
class WindowsInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.cleanup_directory)
        cls.directory = Path(cls.temp.name)
        cls.case_directories = []
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
    def finish_case_processes(cls, directory):
        # Find only executables in this test's exact directory. samefile handles Windows short paths.
        for process in psutil.process_iter(['exe']):
            try:
                executable = process.info['exe']
                if not executable or not os.path.samefile(os.path.dirname(executable), directory):
                    continue
                try:
                    process.wait(timeout=10)
                except psutil.TimeoutExpired:
                    children = process.children(recursive=True)
                    for child in children:
                        child.kill()
                    process.kill()
                    _, alive = psutil.wait_procs([process, *children], timeout=10)
                    if alive:
                        raise RuntimeError('A test executable did not stop: ' + str([p.pid for p in alive]))
            except (psutil.NoSuchProcess, psutil.AccessDenied, FileNotFoundError):
                continue

    @classmethod
    def cleanup_directory(cls):
        for directory in getattr(cls, 'case_directories', []):
            cls.finish_case_processes(directory)
        # A scanner can briefly hold an executable even after its process has exited.
        deadline = time.monotonic() + 5
        while True:
            try:
                cls.temp.cleanup()
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)

    def run_installer(self, kind, wrong_digest=False):
        directory = self.directory / ('wrong-digest' if wrong_digest else kind)
        directory.mkdir()
        self.case_directories.append(directory)
        self.addCleanup(self.finish_case_processes, directory)
        target = directory / 'SIPANTAU.exe'
        target.write_bytes(self.binaries['old'])
        staged = directory / 'SIPANTAU.update.exe'
        staged.write_bytes(self.binaries[kind])
        marker = directory / ('update-health-' + 'b'*32 + '.ready')
        script = directory / 'install.ps1'
        # Use a real process, avoiding invalid/fabricated Windows PIDs and testing parent waiting.
        parent = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(1)'])
        try:
            digest = '0'*64 if wrong_digest else hashlib.sha256(staged.read_bytes()).hexdigest()
            # Reproduce the missing cmdlet even on hosts where it normally works.
            unavailable_hash = "function Get-FileHash { throw 'Get-FileHash is deliberately unavailable' }\n"
            script.write_text(unavailable_hash + installer_script(target, staged, marker, parent.pid, digest, 'v1.2.0'),
                              encoding='utf-8-sig')
            completed = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                        '-File', str(script)], capture_output=True, timeout=75,
                                       text=True, encoding='utf-8', errors='replace')
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            status_path = directory/'update-status.json'
            self.assertTrue(status_path.exists(), completed.stdout + completed.stderr)
            status = json.loads(status_path.read_text(encoding='utf-8-sig'))
        finally:
            if parent.poll() is None:
                parent.terminate()
            parent.wait(timeout=10)
            # Always release the image, including when status/assertions failed.
            self.finish_case_processes(directory)
        return directory, status

    def test_actual_windows_replace_and_readiness(self):
        directory, status = self.run_installer('ready')
        self.assertEqual(status['outcome'], 'installed', status)
        self.assertIsNone(status['error'])
        self.assertEqual((directory/'SIPANTAU.exe').read_bytes(), self.binaries['ready'])
        self.assertEqual((directory/'SIPANTAU.previous.exe').read_bytes(), self.binaries['old'])

    def test_actual_windows_failed_startup_restores_old_executable(self):
        directory, status = self.run_installer('bad')
        self.assertEqual(status['outcome'], 'rolled-back', status)
        self.assertEqual(status['phase'], 'wait-readiness')
        self.assertIn('exited before readiness', status['error'])
        self.assertEqual((directory/'SIPANTAU.exe').read_bytes(), self.binaries['old'])
        self.assertTrue((directory/'rollback-started').exists())

    def test_actual_windows_wrong_digest_keeps_original_and_reports_failure_phase(self):
        directory, status = self.run_installer('ready', wrong_digest=True)
        self.assertEqual(status['outcome'], 'failed', status)
        self.assertEqual(status['phase'], 'verify-download')
        self.assertIn('failed verification', status['error'])
        self.assertEqual((directory/'SIPANTAU.exe').read_bytes(), self.binaries['old'])
        self.assertFalse((directory/'SIPANTAU.previous.exe').exists())

import json
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest import mock

import startup_checks
from scripts.smoke_windows import check_package


class StartupSmokeTests(unittest.TestCase):
    def test_smoke_mode_runs_before_config_and_runtime_controls(self):
        with mock.patch('sys.argv', ['agent.py', '--smoke-test', 'report.json']), mock.patch('startup_checks.smoke_test', return_value=0) as check, mock.patch('client_config.read_config') as config, mock.patch('windows_autorun._powershell') as tasks:
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(Path(__file__).resolve().parent.parent / 'agent.py'), run_name='__main__')
        self.assertEqual(stopped.exception.code, 0)
        check.assert_called_once_with('report.json')
        config.assert_not_called()
        tasks.assert_not_called()

    def test_hidden_tk_and_bundled_assets_report_embedded_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'assets').mkdir()
            (root / 'assets' / 'app.ico').write_bytes(b'test-icon')
            report = root / 'report.json'
            window = mock.Mock()
            with mock.patch.dict('sys.modules', keyboard=mock.Mock(), mss=mock.Mock()), mock.patch.object(startup_checks.sys, '_MEIPASS', str(root), create=True), mock.patch.object(startup_checks.sys, 'frozen', True, create=True), mock.patch('tkinter.Tk', return_value=window), mock.patch('tkinter.Label'), mock.patch('version.VERSION', '1.7.0'), mock.patch('version.BUILD_FORMAT', 'onedir'):
                self.assertEqual(startup_checks.smoke_test(report), 0)
            data = json.loads(report.read_text())
            self.assertEqual(data, {'ok': True, 'version': '1.7.0', 'build_format': 'onedir', 'frozen': True})
            window.withdraw.assert_called_once()
            window.iconbitmap.assert_called_once_with(str(root / 'assets' / 'app.ico'))
            window.destroy.assert_called_once()

    def test_missing_assets_and_tk_failures_are_not_successful_smokes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / 'report.json'
            with mock.patch.dict('sys.modules', keyboard=mock.Mock(), mss=mock.Mock()), mock.patch.object(startup_checks.sys, '_MEIPASS', str(root), create=True), mock.patch('tkinter.Tk') as create:
                self.assertEqual(startup_checks.smoke_test(report), 1)
                self.assertIn('app.ico', json.loads(report.read_text())['error'])
                create.assert_not_called()
                (root / 'assets').mkdir()
                (root / 'assets' / 'app.ico').write_bytes(b'icon')
                create.side_effect = RuntimeError('desktop unavailable')
                self.assertEqual(startup_checks.smoke_test(report), 1)
                self.assertFalse(json.loads(report.read_text())['ok'])

    def test_package_launcher_requires_fresh_report_and_expected_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / 'report.json'
            executable = root / 'SystemObservation.exe'
            expected = {'ok': True, 'frozen': True, 'version': '1.7.0', 'build_format': 'onedir'}
            for field, value in [(None, None), ('ok', False), ('frozen', False), ('version', '1.6.2'), ('build_format', 'source')]:
                with self.subTest(field=field):
                    data = {**expected, **({field: value} if field else {})}
                    report.write_text('stale report')
                    def launch(*args, **kwargs):
                        self.assertFalse(report.exists())
                        self.assertNotEqual(Path(kwargs['cwd']), executable.parent)
                        self.assertEqual(args[0], [str(executable.resolve()), '--smoke-test', str(report.resolve())])
                        report.write_text(json.dumps(data))
                        process = mock.Mock()
                        process.wait.return_value = 0
                        return process
                    with mock.patch('scripts.smoke_windows.subprocess.Popen', side_effect=launch):
                        if field:
                            with self.assertRaises(RuntimeError):
                                check_package(executable, report, 'v1.7.0', 'onedir')
                        else:
                            self.assertEqual(check_package(executable, report, 'v1.7.0', 'onedir'), expected)

    def test_crashed_package_cannot_reuse_a_success_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / 'report.json'
            report.write_text('{"ok":true}')
            process = mock.Mock()
            process.wait.return_value = 1
            with mock.patch('scripts.smoke_windows.subprocess.Popen', return_value=process):
                with self.assertRaisesRegex(RuntimeError, 'exited with 1'):
                    check_package(root / 'app.exe', report, 'v1.7.0', 'onedir')
            self.assertFalse(report.exists())

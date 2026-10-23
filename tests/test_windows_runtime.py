from types import SimpleNamespace
import os
from pathlib import Path
import shutil
import unittest
from unittest import mock
import windows_runtime


class WindowsRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('SIPANTAU_REQUIRE_WINDOWS_TESTS') == '1', 'CI-only native prerequisites')
    def test_ci_cannot_silently_skip_native_windows_checks(self):
        self.assertEqual(os.name, 'nt')
        self.assertIsNotNone(shutil.which('powershell.exe'))
        self.assertIsNotNone(shutil.which('pwsh'))
        compiler = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        self.assertTrue(compiler.is_file(), 'Windows installer tests require the .NET compiler')
        self.assertTrue(windows_runtime.ctypes.windll.shell32.IsUserAnAdmin(),
                        'Packaged UAC startup and autorun task checks require an elevated CI runner')

    def test_initiated_but_unconfirmed_windows_lock_cannot_allow_update(self):
        api = SimpleNamespace(user32=SimpleNamespace(LockWorkStation=mock.Mock(return_value=1)))
        with mock.patch.object(windows_runtime.sys, 'getwindowsversion', create=True, return_value=SimpleNamespace(major=10)), mock.patch.object(windows_runtime.ctypes, 'windll', create=True, new=api):
            self.assertFalse(windows_runtime.lock_for_update(query=lambda: False, sleep=lambda _: None))

    def test_confirmed_windows_lock_allows_update(self):
        api = SimpleNamespace(user32=SimpleNamespace(LockWorkStation=mock.Mock(return_value=1)))
        query = mock.Mock(side_effect=[False, False, True])
        with mock.patch.object(windows_runtime.sys, 'getwindowsversion', create=True, return_value=SimpleNamespace(major=10)), mock.patch.object(windows_runtime.ctypes, 'windll', create=True, new=api):
            self.assertTrue(windows_runtime.lock_for_update(query=query, sleep=lambda _: None))

    def test_denied_windows_lock_postpones_update(self):
        api = SimpleNamespace(user32=SimpleNamespace(LockWorkStation=mock.Mock(return_value=0)))
        with mock.patch.object(windows_runtime.sys, 'getwindowsversion', create=True, return_value=SimpleNamespace(major=10)), mock.patch.object(windows_runtime.ctypes, 'windll', create=True, new=api):
            self.assertFalse(windows_runtime.lock_for_update(query=lambda: False, sleep=lambda _: None))

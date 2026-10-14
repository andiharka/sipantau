from types import SimpleNamespace
import unittest
from unittest import mock
import windows_runtime


class WindowsRuntimeTests(unittest.TestCase):
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

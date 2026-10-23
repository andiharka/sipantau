import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock
import uuid
import xml.etree.ElementTree as ET

import windows_autorun as autorun


SID = 'S-1-5-21-100-200-300-1001'
NS = {'t': autorun.TASK_NS}


class AutorunTests(unittest.TestCase):
    def configure(self, enabled=True, **kwargs):
        return autorun.configure_autorun(enabled, r'C:\Program Files\Observasi & Pustaka\SystemObservation.exe',
                                         r'C:\Program Files\Observasi & Pustaka', **kwargs)

    def test_packaged_task_keeps_command_separate_from_arguments_and_escapes_xml(self):
        with mock.patch.object(autorun, '_powershell', side_effect=[{'user_id': SID, 'elevated': True}, {'ok': True}]) as run, mock.patch.object(autorun, '_remove_legacy_entry') as cleanup:
            self.assertEqual(self.configure(), autorun.TASK_PREFIX + SID)
        payload = run.call_args_list[1].args[1]
        xml = ET.fromstring(payload['xml'])
        self.assertEqual(xml.findtext('t:Actions/t:Exec/t:Command', namespaces=NS), payload['executable'])
        self.assertEqual(xml.findtext('t:Actions/t:Exec/t:WorkingDirectory', namespaces=NS), payload['directory'])
        self.assertEqual(payload['arguments'], '')
        self.assertEqual(xml.findtext('t:Principals/t:Principal/t:LogonType', namespaces=NS), 'InteractiveToken')
        self.assertEqual(xml.findtext('t:Principals/t:Principal/t:RunLevel', namespaces=NS), 'HighestAvailable')
        self.assertEqual(xml.findtext('t:Principals/t:Principal/t:UserId', namespaces=NS), SID)
        self.assertEqual(xml.findtext('t:Triggers/t:LogonTrigger/t:UserId', namespaces=NS), SID)
        self.assertIsNone(xml.find('t:Triggers/t:BootTrigger', NS))
        self.assertEqual(xml.findtext('t:Settings/t:ExecutionTimeLimit', namespaces=NS), 'PT0S')
        for setting in ('DisallowStartIfOnBatteries', 'StopIfGoingOnBatteries', 'RunOnlyIfNetworkAvailable', 'RunOnlyIfIdle'):
            self.assertEqual(xml.findtext('t:Settings/t:' + setting, namespaces=NS), 'false')
        cleanup.assert_called_once()

    def test_source_script_with_spaces_and_unicode_is_quoted_as_one_argument(self):
        script = 'C:\\Agen Pérpus & Lab\\agent.py'
        with mock.patch.object(autorun, '_powershell', side_effect=[{'user_id': SID, 'elevated': True}, {'ok': True}]) as run, mock.patch.object(autorun, '_remove_legacy_entry'):
            self.configure(script=script)
        payload = run.call_args.args[1]
        self.assertEqual(payload['arguments'], '"' + script + '"')
        self.assertEqual(ET.fromstring(payload['xml']).findtext('t:Actions/t:Exec/t:Arguments', namespaces=NS), '"' + script + '"')

    def test_migration_removes_registry_only_after_verified_registration(self):
        events = []

        def run(script, payload):
            if script == autorun.IDENTITY_SCRIPT:
                return {'user_id': SID, 'elevated': True}
            events.append('register')
            return {'ok': True}

        with mock.patch.object(autorun, '_powershell', side_effect=run), mock.patch.object(autorun, '_remove_legacy_entry', side_effect=lambda: events.append('cleanup')):
            self.configure()
        self.assertEqual(events, ['register', 'cleanup'])
        for failure in (autorun.AutorunError('access denied'), {'ok': False}):
            with self.subTest(failure=failure), mock.patch.object(autorun, '_powershell', side_effect=[{'user_id': SID, 'elevated': True}, failure]) as run, mock.patch.object(autorun, '_remove_legacy_entry') as cleanup:
                with self.assertRaises(autorun.AutorunError):
                    self.configure()
                cleanup.assert_not_called()

    def test_wrong_account_and_unelevated_source_run_do_not_register(self):
        for identity in (autorun.AutorunError('Akun administrator berbeda'), {'user_id': SID, 'elevated': False}):
            with self.subTest(identity=identity), mock.patch.object(autorun, '_powershell', side_effect=[identity]) as run, mock.patch.object(autorun, '_remove_legacy_entry') as cleanup:
                with self.assertRaises(autorun.AutorunError):
                    self.configure()
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[1], {'require_interactive': True})
                cleanup.assert_not_called()

    def test_disable_removes_only_this_account_task_and_legacy_value(self):
        with mock.patch.object(autorun, '_powershell', side_effect=[{'user_id': SID, 'elevated': True}, {'ok': True}]) as run, mock.patch.object(autorun, '_remove_legacy_entry') as cleanup:
            self.configure(False)
        self.assertEqual(run.call_args_list[0].args[1], {'require_interactive': False})
        payload = run.call_args.args[1]
        self.assertEqual(payload['task_name'], autorun.TASK_PREFIX + SID)
        self.assertFalse(payload['enabled'])
        self.assertEqual(payload['xml'], '')
        cleanup.assert_called_once()

    def test_legacy_cleanup_is_idempotent_and_preserves_unrelated_entries(self):
        registry = mock.MagicMock()
        with mock.patch.dict('sys.modules', winreg=registry):
            autorun._remove_legacy_entry()
            registry.OpenKey.assert_called_once_with(registry.HKEY_CURRENT_USER, autorun.LEGACY_KEY, 0, registry.KEY_SET_VALUE)
            registry.DeleteValue.assert_called_once_with(registry.OpenKey.return_value.__enter__.return_value, 'SipantauAgent')
            registry.DeleteValue.side_effect = FileNotFoundError
            autorun._remove_legacy_entry()
            registry.OpenKey.side_effect = FileNotFoundError
            autorun._remove_legacy_entry()
            registry.OpenKey.side_effect = PermissionError('denied')
            with self.assertRaises(PermissionError):
                autorun._remove_legacy_entry()

    def test_cleanup_failure_is_reported_even_if_task_was_registered(self):
        with mock.patch.object(autorun, '_powershell', side_effect=[{'user_id': SID, 'elevated': True}, {'ok': True}]), mock.patch.object(autorun, '_remove_legacy_entry', side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(autorun.AutorunError, 'entri startup lama'):
                self.configure()

    def test_powershell_gets_data_on_stdin_without_command_interpolation(self):
        payload = {'directory': "C:\\Pérpus & Lab\\$() '"}
        with mock.patch.object(autorun.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout='{"ok": true}')) as run:
            self.assertEqual(autorun._powershell('fixed script', payload), {'ok': True})
        args, kwargs = run.call_args
        self.assertEqual(base64.b64decode(args[0][-1]).decode('utf-16le'), 'fixed script')
        self.assertEqual(json.loads(kwargs['input']), payload)
        self.assertNotIn('shell', kwargs)
        self.assertEqual(kwargs['timeout'], 30)
        self.assertIn('-NonInteractive', args[0])

    def test_native_errors_timeouts_and_unverifiable_output_are_failures(self):
        for result in (OSError('missing powershell'), subprocess.TimeoutExpired('powershell', 30),
                       mock.Mock(returncode=1, stderr='access denied'),
                       mock.Mock(returncode=0, stdout='not json'), mock.Mock(returncode=0, stdout='[]')):
            with self.subTest(result=result), mock.patch.object(autorun.subprocess, 'run', **({'side_effect': result} if isinstance(result, Exception) else {'return_value': result})):
                with self.assertRaises(autorun.AutorunError):
                    autorun._powershell('fixed script', {})


@unittest.skipUnless(os.name == 'nt', 'Requires Windows Task Scheduler')
class NativeAutorunTests(unittest.TestCase):
    def test_register_read_back_update_and_idempotently_remove_isolated_task(self):
        identity = autorun._powershell(autorun.IDENTITY_SCRIPT, {'require_interactive': False})
        if not identity['elevated']:
            if os.environ.get('SIPANTAU_REQUIRE_WINDOWS_TESTS') == '1':
                self.fail('CI must run the native autorun registration test with elevation')
            self.skipTest('Requires an elevated Windows test runner')
        # No real agent or registry entries are touched. A logon trigger cannot fire
        # during this test; always remove our unique task, including on assertion failure.
        user = identity['user_id']
        name = 'SIPANTAUAgent-Test-' + uuid.uuid4().hex
        executable = sys.executable
        directory = str(Path(sys.executable).parent)
        payload = {'enabled': True, 'task_name': name, 'user_id': user,
                   'executable': executable, 'arguments': '-c "pass"', 'directory': directory}
        try:
            for arguments in ('', '-c "pass"', '-c "print(123)"'):
                payload['arguments'] = arguments
                payload['xml'] = autorun.task_xml(user, executable, arguments, directory)
                result = autorun._powershell(autorun.SCHEDULER_SCRIPT, payload)
                self.assertTrue(result['ok'])
                xml = ET.fromstring(result['xml'])
                self.assertEqual(xml.findtext('t:Actions/t:Exec/t:Arguments', default='', namespaces=NS), arguments)
                self.assertEqual(xml.findtext('t:Settings/t:ExecutionTimeLimit', namespaces=NS), 'PT0S')
        finally:
            payload['enabled'] = False
            self.assertTrue(autorun._powershell(autorun.SCHEDULER_SCRIPT, payload)['ok'])
        self.assertTrue(autorun._powershell(autorun.SCHEDULER_SCRIPT, payload)['ok'])

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from remote_commands import CommandReceipts, RemoteCommands, CommandRejected, CommandTransient, CaptureUnavailable

ID = '12345678-1234-1234-1234-123456789abc'
BOOT = 'a'*32


class RemoteCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 100.0
        self.control = [5, 0, False]
        self.reports = []
        self.capture = mock.Mock(return_value=b'\x89PNG\r\n\x1a\nvalid-image')
        self.upload = mock.Mock()
        self.shutdown = mock.Mock()
        self.notice = mock.Mock()
        self.commit = mock.Mock()
        self.receipts = CommandReceipts(Path(self.temp.name)/'command-receipts.json')
        def report(command_id, status, error=None):
            self.reports.append(status)
            return {'success': True, 'control_version': 6, 'server_now': '2026-10-14T00:00:00Z',
                    'command': {'expiresAt': '2026-10-14T00:02:00Z'}}
        self.manager = RemoteCommands(BOOT, self.receipts, report, self.upload, self.capture,
            self.shutdown, lambda: tuple(self.control), self.notice, self.commit,
            monotonic=lambda: self.now, wait=self.advance)
        self.command = {'id': ID, 'action': 'shutdown', 'boot_id': BOOT, 'expected_version': 5,
                        'expires_at': '2026-10-14T00:00:30Z'}

    def tearDown(self):
        self.temp.cleanup()

    def advance(self, seconds): self.now += seconds

    def enqueue(self, action='shutdown'):
        self.command['action'] = action
        self.manager.observe([self.command], '2026-10-14T00:00:00Z')
        return self.manager.inbox.get_nowait()

    def test_shutdown_warns_then_commits_and_submits_once(self):
        self.manager.process(*self.enqueue())
        self.notice.assert_called_once_with(30)
        self.assertGreaterEqual(self.now, 130)
        self.commit.assert_called_once_with(6, (5, 0, False))
        self.shutdown.assert_called_once()
        self.assertEqual(self.reports, ['accepted', 'executing', 'submitted'])
        self.assertFalse(self.manager.busy.is_set())
        self.manager.observe([self.command], '2026-10-14T00:00:00Z')
        self.assertTrue(self.manager.inbox.empty())
        restored = CommandReceipts(self.receipts.path)
        self.assertTrue(restored.contains(ID))
        self.assertEqual(restored.items[-1]['status'], 'submitted')

    def test_lost_execution_permission_never_shuts_down_or_retries_execution(self):
        original = self.manager.report
        def report(command_id, status, error=None):
            if status == 'executing':
                self.reports.append(status)
                raise CommandTransient('lost response')
            return original(command_id, status, error)
        self.manager.report = report
        self.manager.process(*self.enqueue())
        self.shutdown.assert_not_called()
        self.assertEqual(self.reports.count('executing'), 1)
        self.assertEqual(self.receipts.items[-1]['status'], 'failed')

    def test_new_local_action_during_warning_cancels_shutdown(self):
        def advance(seconds):
            self.now += seconds
            self.control[1] += 1
        self.manager.wait = advance
        self.manager.process(*self.enqueue())
        self.shutdown.assert_not_called()
        self.commit.assert_not_called()
        self.assertNotIn('executing', self.reports)
        self.assertEqual(self.receipts.items[-1]['status'], 'cancelled')

    def test_new_server_control_version_during_warning_cancels_shutdown(self):
        def advance(seconds):
            self.now += seconds
            self.control[0] = 6
        self.manager.wait = advance
        self.manager.process(*self.enqueue())
        self.shutdown.assert_not_called()
        self.assertNotIn('executing', self.reports)

    def test_restart_expired_wrong_boot_and_malformed_commands_are_not_enqueued(self):
        for changes in [{'boot_id': 'b'*32}, {'expires_at': '2026-10-13T00:00:00Z'},
                        {'action': 'shell'}, {'id': '../x'}, {'expected_version': True}]:
            self.manager.observe([{**self.command, **changes}], '2026-10-14T00:00:00Z')
            self.assertTrue(self.manager.inbox.empty())
        self.receipts.write(ID, 'old-boot', 'received')
        self.manager.observe([self.command], '2026-10-14T00:00:00Z')
        self.assertTrue(self.manager.inbox.empty())

    def test_screenshot_upload_retry_reuses_capture_and_command_id(self):
        self.upload.side_effect = [CommandTransient('offline'), {'success': True}]
        self.manager.process(*self.enqueue('screenshot'))
        self.capture.assert_called_once()
        self.assertEqual(self.upload.call_count, 2)
        self.shutdown.assert_not_called()
        self.assertEqual(self.receipts.items[-1]['status'], 'completed')
        self.assertEqual(self.upload.call_args.args[0], ID)

    def test_secure_desktop_capture_failure_reports_reason_and_never_uploads(self):
        self.capture.side_effect = CaptureUnavailable('Windows secure desktop is locked')
        self.manager.report = mock.Mock(side_effect=self.manager.report)
        self.manager.process(*self.enqueue('screenshot'))
        self.upload.assert_not_called()
        self.assertEqual(self.receipts.items[-1]['status'], 'failed')
        self.assertIn('Windows secure desktop', self.manager.report.call_args.args[2])

    def test_oversized_or_invalid_screenshot_is_not_uploaded(self):
        self.capture.return_value = b'not-an-image'
        self.manager.process(*self.enqueue('screenshot'))
        self.upload.assert_not_called()
        self.assertEqual(self.receipts.items[-1]['status'], 'failed')

    def test_receipt_write_failure_prevents_acknowledgment_and_all_os_actions(self):
        with mock.patch.object(self.receipts, 'write', side_effect=OSError('disk full')):
            self.manager.process(*self.enqueue())
        self.assertEqual(self.reports, [])
        self.shutdown.assert_not_called()
        self.capture.assert_not_called()

    def test_corrupt_receipts_disable_commands_instead_of_replaying(self):
        self.receipts.path.write_text('corrupt')
        with self.assertRaises(ValueError): CommandReceipts(self.receipts.path)

    def test_offline_acceptance_timeout_never_runs_os_action(self):
        self.manager.report = mock.Mock(side_effect=CommandTransient('offline'))
        self.manager.process(*self.enqueue())
        self.assertGreaterEqual(self.now, 130)
        self.shutdown.assert_not_called()
        self.capture.assert_not_called()
        self.assertFalse(self.manager.busy.is_set())

    def test_bounded_receipts_retain_only_recent_ids(self):
        for i in range(105): self.receipts.write(str(i), BOOT, 'completed')
        self.assertEqual(len(self.receipts.items), 100)
        self.assertFalse(self.receipts.contains('0'))
        self.assertTrue(self.receipts.contains('104'))

    def test_lost_submitted_ack_does_not_replay_or_claim_shutdown_was_cancelled(self):
        original = self.manager.report
        def report(command_id, status, error=None):
            if status == 'submitted': raise CommandTransient('offline after shutdown')
            return original(command_id, status, error)
        self.manager.report = report
        self.manager.process(*self.enqueue())
        self.shutdown.assert_called_once()
        self.assertEqual(self.receipts.items[-1]['status'], 'submitted')
        self.assertNotIn('cancelled', self.reports)

    def test_old_version_guard_rejects_before_accepted(self):
        item = self.enqueue()
        self.control[0] = 7
        self.manager.process(*item)
        self.assertNotIn('accepted', self.reports)
        self.shutdown.assert_not_called()

    def test_jpeg_capture_is_accepted_and_uploaded(self):
        self.capture.return_value = b'\xff\xd8\xffjpeg\xff\xd9'
        self.manager.process(*self.enqueue('screenshot'))
        self.upload.assert_called_once()
        self.assertEqual(self.receipts.items[-1]['status'], 'completed')

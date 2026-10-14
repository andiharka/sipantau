import copy
import logging
from types import SimpleNamespace
import unittest
from unittest import mock

with mock.patch.object(logging, 'basicConfig'):
    import agent


class AgentCommandIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.old = copy.deepcopy(agent.local_state)
        self.clock = agent.SESSION_CLOCK
        agent.SESSION_CLOCK = agent.SessionClock()
        agent.local_state.update(control_version=5, is_locked=False, pending_actions=[])

    def tearDown(self):
        agent.local_state = self.old
        agent.SESSION_CLOCK = self.clock

    def test_commit_accepts_heartbeat_observing_our_own_authorized_version(self):
        token = agent.command_guard()
        agent.local_state['control_version'] = 6
        with mock.patch.object(agent, 'save_state', return_value=True):
            agent.commit_remote_shutdown(6, token)
        self.assertTrue(agent.local_state['is_locked'])
        self.assertEqual(agent.local_state['control_version'], 6)

    def test_commit_rejects_newer_local_action_or_unsaved_state(self):
        token = agent.command_guard()
        agent.local_state['pending_actions'] = [{'id': 'new'}]
        with self.assertRaises(RuntimeError): agent.commit_remote_shutdown(6, token)
        agent.local_state['pending_actions'] = []
        with mock.patch.object(agent, 'save_state', return_value=False):
            with self.assertRaises(RuntimeError): agent.commit_remote_shutdown(6, token)

    def test_upload_uses_multipart_without_json_content_type(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {'success': True}
        with mock.patch.dict(agent.config, server_url='http://server', api_key='key'), mock.patch.object(agent.requests, 'post', return_value=response) as post:
            agent.upload_remote_screenshot('uuid', b'\x89PNG\r\n\x1a\nimage')
            kwargs = post.call_args.kwargs
            self.assertNotIn('Content-Type', kwargs['headers'])
            self.assertEqual(kwargs['data']['boot_id'], agent.BOOT_ID)
            self.assertEqual(kwargs['files']['image'][2], 'image/png')

    def test_native_shutdown_uses_fixed_non_force_arguments_and_checks_return_code(self):
        with mock.patch.object(agent.os, 'name', 'nt'), mock.patch.object(agent.subprocess, 'CREATE_NO_WINDOW', 0, create=True), mock.patch.object(agent.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            agent.submit_windows_shutdown()
            args = run.call_args.args[0]
            self.assertEqual(args[1:], ['/s', '/t', '0', '/d', 'p:0:0'])
            self.assertNotIn('/f', args)
            run.return_value = SimpleNamespace(returncode=1)
            with self.assertRaises(RuntimeError): agent.submit_windows_shutdown()

    def test_updates_are_disabled_while_remote_command_is_in_progress(self):
        manager = SimpleNamespace(busy=mock.Mock(is_set=lambda: True))
        agent.local_state['is_locked'] = True
        with mock.patch.object(agent, 'COMMAND_MANAGER', manager), mock.patch.object(agent, 'is_locked', True), mock.patch.object(agent, 'lock_health_error', None):
            self.assertFalse(agent.update_allowed())

    def test_update_callback_never_blocks_gui_on_command_worker_lock(self):
        agent.HOST_ACTION_LOCK.acquire()
        try:
            with mock.patch('windows_runtime.lock_for_update') as lock:
                agent.install_release('staged', 'v1.5.0', 'digest')
                lock.assert_not_called()
        finally:
            agent.HOST_ACTION_LOCK.release()

    def test_upload_uses_jpeg_filename_and_mime_for_jpeg_bytes(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {'success': True}
        with mock.patch.dict(agent.config, server_url='http://server', api_key='key'), mock.patch.object(agent.requests, 'post', return_value=response) as post:
            agent.upload_remote_screenshot('uuid', b'\xff\xd8\xffjpeg\xff\xd9')
            image = post.call_args.kwargs['files']['image']
            self.assertEqual(image[0], 'screenshot.jpg')
            self.assertEqual(image[2], 'image/jpeg')

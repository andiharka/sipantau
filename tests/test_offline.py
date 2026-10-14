import copy
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest import mock

with mock.patch.object(logging, 'basicConfig'):
    import agent


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.old = copy.deepcopy(agent.local_state)
        self.clock = agent.SESSION_CLOCK
        self.adoption = agent.LOCAL_ADOPTION
        self.now = 100.0
        agent.SESSION_CLOCK = agent.SessionClock(lambda: self.now)
        agent.LOCAL_ADOPTION = None
        agent.local_state.update(is_locked=True, control_version=5, pending_actions=[], expires_at=None,
                                 server_offset_seconds=0, session_id=None)
        while not agent.GUI_COMMANDS.empty(): agent.GUI_COMMANDS.get_nowait()
        self.config = mock.patch.dict(agent.config, master_password='staff-password', server_url='http://offline-server', api_key='k'*32)
        self.config.start()
        self.save = mock.patch.object(agent, 'save_state')
        self.save.start()

    def tearDown(self):
        self.config.stop()
        self.save.stop()
        agent.local_state = self.old
        agent.SESSION_CLOCK = self.clock
        agent.LOCAL_ADOPTION = self.adoption

    def test_password_required_for_both_modes_and_invalid_duration_does_not_unlock(self):
        for password, minutes in [('wrong', 60), ('wrong', None), ('staff-password', 0),
                                  ('staff-password', 1441), ('staff-password', True), ('staff-password', 1.5)]:
            with self.assertRaises(ValueError): agent.authorize_local_access(password, minutes)
            self.assertTrue(agent.local_state['is_locked'])
        with mock.patch.dict(agent.config, master_password=''):
            with self.assertRaises(ValueError): agent.authorize_local_access('', None)

    def test_timed_unlock_expiry_and_stop_work_without_any_network(self):
        with mock.patch.object(agent.requests, 'post', side_effect=agent.requests.ConnectionError('offline')) as post, mock.patch.object(agent, 'get_running_apps', return_value=[]), mock.patch.object(agent, 'get_local_ip', return_value='127.0.0.1'):
            pending = agent.authorize_local_access('staff-password', 30)
            self.assertFalse(agent.local_state['is_locked'])
            self.assertEqual(agent.SESSION_CLOCK.remaining(), 1800)
            with mock.patch.object(agent, 'hide_lock_screen_gui') as hide:
                agent.apply_local_gui(pending)
                hide.assert_called_once()
            with self.assertRaises(agent.requests.ConnectionError): agent.heartbeat_once()
            self.assertEqual(agent.local_state['pending_actions'][0]['id'], pending)
            self.now += 1800
            self.assertTrue(agent.expire_local_session())
            self.assertTrue(agent.local_state['is_locked'])
            with mock.patch.object(agent, 'show_lock_screen_gui') as show:
                agent.GUI_COMMANDS.get_nowait()()
                show.assert_called_once()
            pending = agent.authorize_local_access('staff-password', 60)
            with mock.patch.object(agent.messagebox, 'askyesno', return_value=True), mock.patch.object(agent, 'show_lock_screen_gui'):
                agent.stop_session_action()
            self.assertTrue(agent.local_state['is_locked'])
            self.assertEqual(post.call_count, 2)

    def test_permanent_unlock_does_not_need_network_or_create_timer(self):
        with mock.patch.object(agent.requests, 'post') as post:
            agent.authorize_local_access('staff-password')
            post.assert_not_called()
        self.assertFalse(agent.local_state['is_locked'])
        self.assertIsNone(agent.SESSION_CLOCK.remaining())

    def test_reconnect_sends_remaining_time_and_preserves_deadline(self):
        agent.authorize_local_access('staff-password', 60)
        expiry = agent.local_state['expires_at']
        self.now += 1200
        response = mock.Mock(status_code=200)
        response.json.return_value = {'control_version': 6}
        with mock.patch.object(agent.requests, 'post', return_value=response) as post:
            agent.flush_pending_action('http://server', {}, True)
            payload = post.call_args.kwargs['json']
            self.assertEqual(payload['remaining_seconds'], 2400)
            self.assertEqual(payload['expires_at'], expiry)
        # Even an inconsistent server clock cannot extend the local monotonic deadline on adoption.
        agent.accept_server_state({'control_version': 6, 'is_locked': False, 'session_id': 20,
                                   'expires_at': expiry, 'server_now': agent.local_state['from_when']}, 0)
        self.assertEqual(agent.SESSION_CLOCK.remaining(), 2400)

    def test_offline_state_reload_and_expired_restart_fail_closed(self):
        agent.authorize_local_access('staff-password', 60)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text(json.dumps(agent.local_state))
            with mock.patch.object(agent, 'state_path', str(path)):
                agent.SESSION_CLOCK.clear()
                agent.load_state()
                self.assertGreater(agent.SESSION_CLOCK.remaining(), 3500)
                self.assertEqual(agent.local_state['pending_actions'][0]['action'], 'start-session')
                saved = json.loads(path.read_text())
                saved['expires_at'] = '2000-01-01T00:00:00Z'
                path.write_text(json.dumps(saved))
                agent.load_state()
                self.assertTrue(agent.expire_local_session())
                self.assertTrue(agent.local_state['is_locked'])
                path.write_text('{corrupted')
                agent.load_state()
                self.assertTrue(agent.local_state['is_locked'])
                self.assertIsNone(agent.SESSION_CLOCK.remaining())

    def test_cached_sites_sync_offline_preserves_unmanaged_hosts_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            hosts = Path(directory) / 'hosts'
            hosts.write_text('127.0.0.1 localhost\n127.0.0.1 example.com # administrator\n127.0.0.1 www.example.com # agent-blocked\n')
            with mock.patch.object(agent, 'hosts_path', str(hosts)):
                agent.local_state['blocked_sites'] = ['blocked.example']
                with mock.patch.object(agent.requests, 'get', side_effect=AssertionError('network not allowed')):
                    agent.restore_cached_site_policy()
                text = hosts.read_text()
                self.assertIn('127.0.0.1 example.com # administrator', text)
                self.assertNotIn('www.example.com', text)
                self.assertIn('127.0.0.1 blocked.example # agent-blocked', text)
                agent.sync_hosts_file([])
                self.assertNotIn('blocked.example', hosts.read_text())

    def test_cached_application_blocking_continues_offline(self):
        class Stop(BaseException): pass
        process = mock.Mock(info={'name': 'BLOCKED.EXE'})
        agent.local_state['blocked_apps'] = ['blocked.exe']
        with mock.patch.object(agent.psutil, 'process_iter', return_value=[process]), mock.patch.object(agent.time, 'sleep', side_effect=Stop):
            with self.assertRaises(Stop): agent.blacklist_watcher()
        process.kill.assert_called_once()

    def test_failed_update_does_not_change_control_or_cached_policy(self):
        from updater import ReleaseUpdater
        updater = ReleaseUpdater({}, '.', '1.0.0', agent.enqueue_gui, lambda: True, mock.Mock())
        updater.enabled = True
        before = copy.deepcopy(agent.local_state)
        with mock.patch('updater.requests.Session.get', side_effect=agent.requests.ConnectionError):
            with self.assertRaises(agent.requests.ConnectionError): updater.check_once()
        self.assertEqual(agent.local_state, before)

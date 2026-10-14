import copy
import logging
import unittest
from unittest import mock

with mock.patch.object(logging, 'basicConfig'):
    import agent


class AcknowledgmentTests(unittest.TestCase):
    def setUp(self):
        self.old_state = copy.deepcopy(agent.local_state)
        self.old_clock = agent.SESSION_CLOCK
        agent.SESSION_CLOCK = agent.SessionClock(lambda: 100)
        agent.local_state = {**copy.deepcopy(self.old_state), 'control_version': 5,
                             'is_locked': True, 'pending_actions': [],
                             'control_error': None, 'applied_control_version': None}
        while not agent.GUI_COMMANDS.empty(): agent.GUI_COMMANDS.get_nowait()

    def tearDown(self):
        agent.local_state = self.old_state
        agent.SESSION_CLOCK = self.old_clock

    def test_acknowledges_only_after_gui_success(self):
        with mock.patch.object(agent, 'save_state'), mock.patch.object(agent, 'show_lock_screen_gui') as show:
            agent.apply_gui_control(5)
            show.assert_called_once()
        self.assertEqual(agent.local_state['applied_control_version'], 5)
        self.assertEqual(agent.local_state['attempted_control_version'], 5)
        self.assertIsNone(agent.local_state['control_error'])

    def test_gui_failure_is_reported_without_success_ack(self):
        with mock.patch.object(agent, 'save_state'), mock.patch.object(agent, 'show_lock_screen_gui', side_effect=RuntimeError('Hook failed')):
            agent.apply_gui_control(5)
        self.assertIsNone(agent.local_state['applied_control_version'])
        self.assertEqual(agent.local_state['control_error'], 'Hook failed')

    def test_old_queued_gui_command_cannot_override_new_session(self):
        with mock.patch.object(agent, 'show_lock_screen_gui') as show:
            agent.apply_gui_control(4)
            show.assert_not_called()

    def test_expiry_callback_does_not_lock_replacement_session(self):
        agent.local_state['is_locked'] = False
        agent.SESSION_CLOCK.sync(5, 1, '2026-10-14T10:00:00Z', '2026-10-14T10:01:00Z')
        with mock.patch.object(agent, 'save_state'):
            self.assertTrue(agent.expire_local_session())
            # A fresh session arrives before Tk processes the queued expiry.
            agent.local_state.update(control_version=6, is_locked=False, pending_actions=[])
            agent.SESSION_CLOCK.sync(6, 2, '2026-10-14T10:10:00Z', '2026-10-14T10:00:00Z')
            with mock.patch.object(agent, 'show_lock_screen_gui') as show:
                agent.GUI_COMMANDS.get_nowait()()
                show.assert_not_called()
            self.assertFalse(agent.expire_local_session())

    def test_cached_offline_lock_is_restored_while_action_is_pending(self):
        agent.local_state['pending_actions'] = [{'id': 'offline', 'action': 'force-lock', 'expected_version': 5}]
        with mock.patch.object(agent, 'save_state'), mock.patch.object(agent, 'show_lock_screen_gui') as show:
            agent.restore_gui_state()
            show.assert_called_once()
        self.assertIsNone(agent.local_state['applied_control_version'])

    def test_new_server_decision_supersedes_offline_action(self):
        agent.local_state['pending_actions'] = [{'id': 'old', 'action': 'force-lock', 'expected_version': 5}]
        with mock.patch.object(agent, 'save_state'):
            agent.accept_server_state({'control_version': 6, 'is_locked': False, 'server_now': '2026-10-14T10:00:00Z',
                                       'expires_at': '2026-10-14T10:10:00Z', 'session_id': 20}, 0)
        self.assertEqual(agent.local_state['pending_actions'], [])
        self.assertFalse(agent.local_state['is_locked'])
        self.assertEqual(agent.SESSION_CLOCK.remaining(), 600)

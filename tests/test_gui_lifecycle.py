import copy
import logging
from pathlib import Path
import tempfile
import unittest
from unittest import mock

with mock.patch.object(logging, 'basicConfig'):
    import agent


class GuiLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.state = copy.deepcopy(agent.local_state)
        self.clock = agent.SESSION_CLOCK
        self.old_window = agent.lock_window
        self.old_timer = agent.timer_window
        self.old_locked = agent.is_locked
        self.old_error = agent.lock_health_error
        agent.SESSION_CLOCK = agent.SessionClock(lambda: 100)
        agent.local_state.update(is_locked=True, control_version=5, pending_actions=[],
                                 control_error=None, session_id=None, expires_at=None)
        agent.lock_window = None
        agent.timer_window = None
        agent.is_locked = True
        agent.lock_health_error = None
        self.save = mock.patch.object(agent, 'save_state', return_value=True)
        self.save.start()

    def tearDown(self):
        self.save.stop()
        agent.local_state = self.state
        agent.SESSION_CLOCK = self.clock
        agent.lock_window = self.old_window
        agent.timer_window = self.old_timer
        agent.is_locked = self.old_locked
        agent.lock_health_error = self.old_error
        while not agent.GUI_COMMANDS.empty():
            agent.GUI_COMMANDS.get_nowait()

    def window(self):
        window = mock.Mock()
        window._regrab_job = 'regrab-123'
        window._status_job = 'status-123'
        window.winfo_exists.return_value = True
        agent.lock_window = window
        return window

    def test_fast_ack_cannot_skip_timed_or_permanent_gui_unlock(self):
        with mock.patch.dict(agent.config, master_password='staff-password'):
            for duration in (30, None):
                with self.subTest(duration=duration):
                    window = self.window()
                    agent.is_locked = True
                    pending = agent.authorize_local_access('staff-password', duration)
                    # Server acknowledges before the button's GUI application step.
                    agent.local_state.update(pending_actions=[], control_version=6)
                    self.assertTrue(agent.apply_local_gui(pending))
                    window.destroy.assert_called_once()
                    self.assertIsNone(agent.lock_window)
                    self.assertFalse(agent.is_locked)
                    self.assertIsNone(agent.local_state['control_error'])

    def test_newer_server_lock_is_preserved_when_local_request_has_already_been_acknowledged(self):
        with mock.patch.dict(agent.config, master_password='staff-password'):
            pending = agent.authorize_local_access('staff-password')
        agent.local_state.update(is_locked=True, pending_actions=[], control_version=7)
        with mock.patch.object(agent, 'show_lock_screen_gui') as show, mock.patch.object(agent, 'hide_lock_screen_gui') as hide:
            self.assertTrue(agent.apply_local_gui(pending))
            show.assert_called_once()
            hide.assert_not_called()
        self.assertTrue(agent.local_state['is_locked'])

    def test_grab_release_failure_cannot_leave_overlay_visible(self):
        window = self.window()
        window.grab_release.side_effect = agent.tk.TclError('grab already released')
        agent.hide_lock_screen_gui()
        window.destroy.assert_called_once()
        self.assertTrue(window._closing)
        window.after_cancel.assert_any_call('regrab-123')
        window.after_cancel.assert_any_call('status-123')
        self.assertIsNone(agent.lock_window)
        self.assertFalse(agent.is_locked)

    def test_overlay_is_removed_even_if_internal_locked_flag_is_false(self):
        window = self.window()
        agent.is_locked = False
        agent.hide_lock_screen_gui()
        window.destroy.assert_called_once()
        self.assertIsNone(agent.lock_window)

    def test_repeated_lock_requests_reuse_the_existing_window(self):
        window = self.window()
        with mock.patch.object(agent.tk, 'Toplevel') as create:
            agent.show_lock_screen_gui()
            agent.show_lock_screen_gui()
            create.assert_not_called()
            self.assertIs(agent.lock_window, window)
        agent.lock_health_error = 'Hook failed'
        with mock.patch.object(agent.tk, 'Toplevel') as create:
            with self.assertRaisesRegex(RuntimeError, 'Hook failed'):
                agent.show_lock_screen_gui()
            create.assert_not_called()

    def test_failed_unlock_records_current_error_and_suppresses_countdown(self):
        with mock.patch.dict(agent.config, master_password='staff-password'):
            pending = agent.authorize_local_access('staff-password', 30)
        with mock.patch.object(agent, 'hide_lock_screen_gui', side_effect=RuntimeError('Cannot dismiss overlay')):
            self.assertFalse(agent.apply_local_gui(pending))
        self.assertEqual(agent.local_state['control_error'], 'Cannot dismiss overlay')
        with mock.patch.object(agent, 'root', mock.Mock()), mock.patch.object(agent, 'show_timer_window_gui') as show:
            agent.update_timer_gui()
            show.assert_not_called()

    def test_config_failure_warns_with_exact_path_before_startup(self):
        parent = mock.Mock()
        with mock.patch.object(agent, 'CONFIG_ERROR', 'File config.json tidak ditemukan.'), mock.patch.object(agent, 'config_path', 'C:/PC-01/config.json'), mock.patch.object(agent.messagebox, 'showwarning') as warning:
            self.assertFalse(agent.startup_config_ready(parent))
            text = warning.call_args.args[1]
            self.assertIn('C:/PC-01/config.json', text)
            self.assertIn('tidak ditemukan', text)
            self.assertIs(warning.call_args.kwargs['parent'], parent)
        with mock.patch.object(agent, 'CONFIG_ERROR', None), mock.patch.object(agent.messagebox, 'showwarning') as warning:
            self.assertTrue(agent.startup_config_ready(parent))
            warning.assert_not_called()

    def test_frozen_config_path_uses_executable_directory_and_loads_once(self):
        old_error, old_loaded_at = agent.CONFIG_ERROR, agent.CONFIG_LOADED_AT
        self.addCleanup(setattr, agent, 'CONFIG_ERROR', old_error)
        self.addCleanup(setattr, agent, 'CONFIG_LOADED_AT', old_loaded_at)
        with tempfile.TemporaryDirectory() as directory:
            exe = str(Path(directory) / 'SIPANTAU.exe')
            with mock.patch.object(agent.sys, 'frozen', True, create=True), mock.patch.object(agent.sys, 'executable', exe), mock.patch.object(agent, 'read_config', return_value={'master_password': 'private'}) as read:
                loaded, path = agent.load_config()
                self.assertEqual(path, str(Path(directory) / 'config.json'))
                read.assert_called_once_with(path)
                self.assertEqual(loaded['master_password'], 'private')
                self.assertIsNone(agent.CONFIG_ERROR)
                self.assertIsNotNone(agent.CONFIG_LOADED_AT)

    def test_timer_uses_seconds_and_warns_at_expected_thresholds(self):
        self.assertEqual(agent.timer_display(3600)[0], '01:00:00')
        self.assertEqual(agent.timer_display(1800)[0], '30:00')
        self.assertEqual(agent.timer_display(0.1)[0], '00:01')
        self.assertFalse(agent.timer_display(600)[3])
        self.assertTrue(agent.timer_display(599)[3])
        self.assertIn('segera', agent.timer_display(179)[2])
        self.assertEqual(agent.TIMER_PRESETS, ((30, '30 menit'), (60, '1 jam'), (180, '3 jam')))

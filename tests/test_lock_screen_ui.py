"""Exercise real form callbacks with in-memory Tk widgets; no desktop or hooks are touched."""
import copy
import logging
import unittest
from unittest import mock

with mock.patch.object(logging, 'basicConfig'):
    import agent


class Variable:
    def __init__(self, value=''):
        self.value = value
        self.watchers = []

    def get(self):
        return self.value

    def set(self, value):
        self.value = str(value)
        for callback in self.watchers:
            callback('', '', '')

    def trace_add(self, mode, callback):
        self.watchers.append(callback)


class Widget:
    focus = None

    def __init__(self, parent=None, **props):
        self.parent = parent
        self.children = []
        self.props = props
        self.bindings = {}
        self.jobs = {}
        self.alive = True
        self.value = ''
        self.items = {}
        self.grabbed = False
        self.placement = ''
        if parent:
            parent.children.append(self)

    def config(self, **props): self.props.update(props)
    configure = config
    def cget(self, field): return self.props.get(field)
    def pack(self, **props): self.placement = 'pack'
    def pack_forget(self): self.placement = ''
    def place(self, **props): self.placement = 'place'
    def bind(self, name, callback, add=None): self.bindings[name] = callback
    def unbind(self, name): self.bindings.pop(name, None)
    def winfo_exists(self): return self.alive
    def winfo_screenwidth(self): return 1366
    def winfo_screenheight(self): return 768
    def winfo_width(self): return self.props.get('width', 248)
    def winfo_height(self): return self.props.get('height', 104)
    def winfo_x(self): return 100
    def winfo_y(self): return 24
    def winfo_rootx(self): return 100
    def winfo_rooty(self): return 24
    def winfo_pointerxy(self): return (0, 0)
    def attributes(self, *args): pass
    def overrideredirect(self, value): pass
    def protocol(self, *args): pass
    def title(self, value): self.props["title"] = value
    def iconbitmap(self, value): self.props["icon"] = value
    def transient(self, parent): pass
    def resizable(self, *args): pass
    def geometry(self, value): self.placement = value
    def lift(self): pass
    def focus_set(self): Widget.focus = self
    focus_force = focus_set
    def focus_displayof(self): return Widget.focus
    def winfo_toplevel(self):
        current = self
        while current.parent and not isinstance(current, Window):
            current = current.parent
        return current
    def grab_set(self): self.grabbed = True
    def grab_release(self): self.grabbed = False
    def after(self, milliseconds, callback):
        job = 'job-' + str(len(self.jobs))
        self.jobs[job] = callback
        return job
    def after_cancel(self, job): self.jobs.pop(job, None)
    def destroy(self):
        self.alive = False
        for child in self.children:
            child.destroy()
    def insert(self, index, value): self.value += value
    def delete(self, *args):
        if args and args[0] == 'icon':
            return
        self.value = ''
    def get(self):
        variable = self.props.get('textvariable')
        return variable.get() if variable else self.value
    def invoke(self): return self.props['command']()
    def set(self, *args): pass
    def yview(self, *args): pass
    def create(self, *args, **props):
        item = len(self.items) + 1
        self.items[item] = props
        return item
    create_line = create
    create_oval = create
    create_text = create
    create_rectangle = create
    def itemconfig(self, item, **props): self.items[item].update(props)
    def coords(self, *args): pass


class Window(Widget):
    pass


def descendants(widget):
    for child in widget.children:
        yield child
        yield from descendants(child)


class LockScreenUiTests(unittest.TestCase):
    def setUp(self):
        self.old = {name: getattr(agent, name) for name in ('root', 'lock_window', 'timer_window', 'is_locked',
                                                          'lock_health_error', 'SESSION_CLOCK', 'local_state', 'UPDATE_CHECK_LOCK')}
        self.addCleanup(self.restore)
        agent.UPDATE_CHECK_LOCK = agent.threading.Lock()
        while not agent.GUI_COMMANDS.empty():
            agent.GUI_COMMANDS.get_nowait()
        agent.root = Window()
        agent.lock_window = None
        agent.timer_window = None
        agent.is_locked = False
        agent.lock_health_error = None
        agent.SESSION_CLOCK = agent.SessionClock(lambda: 100)
        agent.local_state = {**copy.deepcopy(self.old['local_state']), 'is_locked': True, 'control_version': 5,
                             'pending_actions': [], 'server_offset_seconds': 0}
        self.tk = mock.patch.multiple(agent.tk, Toplevel=Window, Frame=Widget, Label=Widget,
            Entry=Widget, Canvas=Widget, Button=Widget, Spinbox=Widget, StringVar=Variable,
            Scrollbar=Widget, Text=Widget)
        self.tk.start()
        self.addCleanup(self.tk.stop)
        self.save = mock.patch.object(agent, 'save_state', return_value=True)
        self.save.start()
        self.addCleanup(self.save.stop)
        self.keyboard = mock.patch.object(agent, 'keyboard', mock.Mock())
        self.keyboard.start()
        self.addCleanup(self.keyboard.stop)
        self.config = mock.patch.dict(agent.config, server_url='http://192.168.1.5:3333',
            api_key='private-api-key-' + 'x'*32 + '1234', master_password='staff-private', hostname='PC-01')
        self.config.start()
        self.addCleanup(self.config.stop)
        agent.show_lock_screen_gui()
        self.window = agent.lock_window
        self.widgets = list(descendants(self.window))
        self.password = next(widget for widget in self.widgets if widget.props.get('show') == '•')

    def restore(self):
        while not agent.GUI_COMMANDS.empty():
            agent.GUI_COMMANDS.get_nowait()
        for name, value in self.old.items():
            setattr(agent, name, value)

    def button(self, text):
        return next(widget for widget in self.widgets if widget.props.get('text') == text)

    def test_wrong_then_correct_password_clears_error_and_dismisses_overlay_for_both_modes(self):
        self.password.value = 'wrong'
        self.button('Mulai sesi dengan timer').invoke()
        error = self.button('Password petugas salah.')
        self.assertTrue(self.window.alive)
        self.password.value = 'staff-private'
        self.button('Mulai sesi dengan timer').invoke()
        self.assertFalse(self.window.alive)
        self.assertEqual(error.props['text'], '')
        self.assertFalse(agent.is_locked)
        self.assertEqual(agent.SESSION_CLOCK.remaining(), 1800)
        agent.update_timer_gui()
        self.assertEqual(agent.timer_window.time_label.props['text'], '30:00')
        agent.dismiss_timer_window()
        agent.show_lock_screen_gui()
        self.window = agent.lock_window
        self.widgets = list(descendants(self.window))
        password = next(widget for widget in self.widgets if widget.props.get('show') == '•')
        password.value = 'staff-private'
        self.button('Buka tanpa batas waktu').invoke()
        self.assertFalse(self.window.alive)
        self.assertFalse(agent.is_locked)
        self.assertIsNone(agent.SESSION_CLOCK.remaining())

    def test_eye_toggle_preserves_typed_password_and_supports_keyboard(self):
        eye = next(widget for widget in self.widgets if widget.props.get('width') == 40)
        self.password.value = 'typed-secret'
        eye.bindings['<Button-1>'](None)
        self.assertEqual(self.password.props['show'], '')
        self.assertEqual(self.password.value, 'typed-secret')
        eye.bindings['<space>'](None)
        self.assertEqual(self.password.props['show'], '•')
        eye.bindings['<Return>'](None)
        self.assertEqual(self.password.props['show'], '')

    def test_presets_set_30_60_and_180_minutes_without_brand_name(self):
        duration = next(widget for widget in self.widgets if 'textvariable' in widget.props)
        self.assertEqual(duration.get(), '30')
        self.assertIsNotNone(self.button(f'Versi {agent.VERSION}'))
        self.button('1 jam').invoke()
        self.assertEqual(duration.get(), '60')
        self.button('3 jam').invoke()
        self.assertEqual(duration.get(), '180')
        self.button('30 menit').invoke()
        self.assertEqual(duration.get(), '30')
        for widget in self.widgets:
            self.assertNotIn('sipantau', str(widget.props.get('text', '')).lower())

    def test_hidden_settings_reveals_masked_viewer_without_unlocking(self):
        settings = next(widget for widget in self.widgets if widget.props.get('width') == 44)
        gear = next(iter(settings.items.values()))
        self.assertEqual(gear['fill'], agent.UI_BG)
        settings.bindings['<Enter>'](None)
        self.assertEqual(gear['fill'], agent.UI_MUTED)
        settings.bindings['<Button-1>'](None)
        dialog = self.window._settings_dialog
        text = '\n'.join(widget.value for widget in descendants(dialog))
        self.assertIn('••••1234', text)
        self.assertNotIn(agent.config['api_key'], text)
        self.assertNotIn(agent.config['master_password'], text)
        self.assertIn('Pendaftaran saat masuk', text)
        self.assertIn(agent.AUTORUN_STATUS, text)
        self.assertTrue(agent.is_locked)
        self.assertTrue(dialog.grabbed)
        close = next(widget for widget in descendants(dialog) if widget.props.get('text') == 'Tutup')
        close.invoke()
        self.assertFalse(dialog.alive)
        self.assertTrue(self.window.grabbed)

    def test_settings_show_failed_registration_even_when_autorun_is_configured(self):
        with mock.patch.dict(agent.config, autorun=True), mock.patch.object(agent, 'AUTORUN_STATUS', 'Gagal · Task Scheduler denied'):
            agent.show_configuration_viewer(self.window)
        text = '\n'.join(widget.value for widget in descendants(self.window._settings_dialog))
        self.assertIn('Jalankan saat masuk\nAktif', text)
        self.assertIn('Pendaftaran saat masuk\nGagal · Task Scheduler denied', text)

    def test_delayed_focus_callback_cannot_revive_a_dismissed_window(self):
        self.window.bindings['<FocusOut>'](None)
        callback = self.window.jobs[self.window._regrab_job]
        agent.hide_lock_screen_gui()
        focus = Widget.focus
        callback()
        self.assertFalse(self.window.alive)
        self.assertFalse(self.window.grabbed)
        self.assertIs(Widget.focus, focus)

    def update_controls(self):
        agent.show_configuration_viewer(self.window)
        dialog = self.window._settings_dialog
        widgets = list(descendants(dialog))
        button = next(widget for widget in widgets if widget.props.get('text') == 'Periksa pembaruan')
        status = next(widget for widget in widgets if str(widget.props.get('text', '')).startswith('Versi saat ini:'))
        return dialog, button, status

    def start_check(self, button):
        with mock.patch.object(agent.threading, 'Thread') as thread:
            button.invoke()
            thread.return_value.start.assert_called_once()
            return thread.call_args.kwargs['target']

    def test_manual_check_runs_in_background_and_reports_available_or_current_version(self):
        dialog, button, status = self.update_controls()
        for selected, expected in [(('v9.0.0', {}), 'Pembaruan tersedia: v9.0.0'), (None, 'sudah yang terbaru')]:
            with self.subTest(selected=selected), mock.patch.dict(agent.config, auto_update=False), mock.patch('updater.check_for_update', return_value=selected) as check:
                worker = self.start_check(button)
                check.assert_not_called()
                self.assertEqual(button.props['state'], 'disabled')
                worker()
                self.assertEqual(status.props['text'], 'Memeriksa rilis terbaru…')
                agent.GUI_COMMANDS.get_nowait()()
                self.assertIn(expected, status.props['text'])
                self.assertEqual(button.props['state'], 'normal')
                self.assertTrue(agent.is_locked)

    def test_manual_check_reports_offline_rate_limit_and_bad_metadata_without_leaking_error_text(self):
        dialog, button, status = self.update_controls()
        response = agent.requests.Response()
        response.status_code = 429
        error = agent.requests.HTTPError('PRIVATE-ERROR-TEXT', response=response)
        cases = [(agent.requests.ConnectionError('PRIVATE-ERROR-TEXT'), 'koneksi internet'),
                 (agent.requests.Timeout('PRIVATE-ERROR-TEXT'), 'koneksi internet'),
                 (error, 'membatasi permintaan'), (ValueError('PRIVATE-ERROR-TEXT'), 'diverifikasi')]
        for failure, expected in cases:
            with self.subTest(error=type(failure).__name__), mock.patch('updater.check_for_update', side_effect=failure):
                worker = self.start_check(button)
                worker()
                agent.GUI_COMMANDS.get_nowait()()
                self.assertIn(expected, status.props['text'])
                self.assertNotIn('PRIVATE-ERROR-TEXT', status.props['text'])
                self.assertEqual(button.props['state'], 'normal')
                self.assertTrue(agent.is_locked)

    def test_closed_settings_discard_results_and_parallel_checks_are_bounded(self):
        dialog, button, status = self.update_controls()
        worker = self.start_check(button)
        close = next(widget for widget in descendants(dialog) if widget.props.get('text') == 'Tutup')
        close.invoke()
        other_dialog, other_button, other_status = self.update_controls()
        with mock.patch.object(agent.threading, 'Thread') as thread:
            other_button.invoke()
            thread.assert_not_called()
            self.assertIn('sedang berjalan', other_status.props['text'])
        with mock.patch('updater.check_for_update', return_value=None):
            worker()
        agent.GUI_COMMANDS.get_nowait()()
        self.assertEqual(status.props['text'], 'Memeriksa rilis terbaru…')
        self.assertIn('sedang berjalan', other_status.props['text'])
        self.assertFalse(dialog.alive)
        self.assertTrue(other_dialog.alive)
        self.assertFalse(agent.UPDATE_CHECK_LOCK.locked())

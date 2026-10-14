"""Bounded, non-replaying one-shot commands delivered by Pantau heartbeats."""
import json
import logging
import os
from pathlib import Path
import queue
import re
import threading
import time
from datetime import timedelta
from control_runtime import utc
from screenshot_codec import image_type

MAX_IMAGE_BYTES = 10 * 1024 * 1024


class CommandRejected(Exception):
    pass


class CommandTransient(Exception):
    pass


class CaptureUnavailable(Exception):
    pass


class CommandReceipts:
    def __init__(self, path):
        self.path = Path(path)
        self.items = []
        if self.path.exists():
            # Do not accept commands when their durable receipt cache is unreadable.
            loaded = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(loaded, list) or any(not isinstance(item, dict) or not isinstance(item.get('id'), str) for item in loaded):
                raise ValueError('Invalid command receipt cache')
            self.items = loaded[-100:]

    def contains(self, command_id):
        return any(item['id'] == command_id for item in self.items)

    def write(self, command_id, boot_id, status):
        updated = [item for item in self.items if item['id'] != command_id]
        updated.append({'id': command_id, 'boot_id': boot_id, 'status': status})
        updated = updated[-100:]
        temp = self.path.with_suffix('.tmp')
        with temp.open('w', encoding='utf-8') as output:
            json.dump(updated, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, self.path)
        self.items = updated


class RemoteCommands:
    def __init__(self, boot_id, receipts, report, upload, capture, shutdown, guard, notice,
                 commit, action_lock=None, monotonic=time.monotonic, wait=None):
        self.boot_id = boot_id
        self.receipts = receipts
        self.report = report
        self.upload = upload
        self.capture = capture
        self.shutdown = shutdown
        self.guard = guard
        self.notice = notice
        self.commit = commit
        self.action_lock = action_lock or threading.Lock()
        self.monotonic = monotonic
        self.stop = threading.Event()
        self.wait = wait or self.stop.wait
        self.busy = threading.Event()
        self.inbox = queue.Queue(maxsize=1)
        self.selection_lock = threading.Lock()

    def observe(self, commands, server_now):
        if not isinstance(commands, list) or len(commands) > 1:
            return
        for command in commands:
            if not isinstance(command, dict) or not re.fullmatch(r'[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}', str(command.get('id', ''))):
                continue
            if command.get('boot_id') != self.boot_id or command.get('action') not in ('shutdown', 'screenshot'):
                continue
            version = command.get('expected_version')
            if isinstance(version, bool) or not isinstance(version, int) or version < 0:
                continue
            try:
                remaining = (utc(command['expires_at']) - utc(server_now)).total_seconds()
                if not 0 < remaining <= 125:
                    continue
            except (ValueError, KeyError, TypeError):
                continue
            with self.selection_lock:
                if self.busy.is_set() or self.receipts.contains(command['id']):
                    continue
                self.busy.set()
                self.inbox.put_nowait((dict(command), self.monotonic() + remaining, self.guard()))

    def _retry(self, callback, deadline):
        while self.monotonic() < deadline and not self.stop.is_set():
            try:
                return callback()
            except CommandTransient:
                self.wait(min(2, max(0, deadline - self.monotonic())))
        raise CommandRejected('Perintah melewati batas waktu koneksi.')

    def _valid_shutdown(self, command, token):
        return self.guard() == token and token[0] == command['expected_version'] and token[2] is False

    def process(self, command, deadline, token):
        command_id = command['id']
        recorded = False
        submitted = False
        try:
            # Persist before acknowledgment or any OS effect. Restart never replays this UUID.
            self.receipts.write(command_id, self.boot_id, 'received')
            recorded = True
            if command['action'] == 'shutdown' and not self._valid_shutdown(command, token):
                raise CommandRejected('Ada keputusan kontrol atau akses petugas baru.')
            accepted = self._retry(lambda: self.report(command_id, 'accepted'), deadline)
            remaining = (utc(accepted['command']['expiresAt']) - utc(accepted['server_now'])).total_seconds()
            if not 0 < remaining <= 125:
                raise CommandRejected('Batas waktu perintah tidak valid.')
            accepted_tick = self.monotonic()
            deadline = accepted_tick + remaining
            self.receipts.write(command_id, self.boot_id, 'accepted')
            with self.action_lock:
                if command['action'] == 'screenshot':
                    image = self.capture()
                    if not isinstance(image, bytes) or len(image) > MAX_IMAGE_BYTES:
                        raise ValueError('Screenshot tidak valid atau melebihi 10 MiB.')
                    image_type(image)
                    captured_at = (utc(accepted['server_now']) + timedelta(seconds=self.monotonic() - accepted_tick)).isoformat()
                    self._retry(lambda: self.upload(command_id, image, captured_at), deadline)
                    self.receipts.write(command_id, self.boot_id, 'completed')
                else:
                    self.notice(30)
                    warning_until = self.monotonic() + 30
                    while self.monotonic() < warning_until:
                        if self.stop.is_set() or self.monotonic() >= deadline or not self._valid_shutdown(command, token):
                            raise CommandRejected('Shutdown dibatalkan karena kontrol PC berubah.')
                        self.wait(min(0.25, warning_until - self.monotonic()))
                    if not self._valid_shutdown(command, token):
                        raise CommandRejected('Kontrol PC berubah.')
                    # Execution permission is deliberately never retried after a lost response.
                    permission = self.report(command_id, 'executing')
                    version = permission.get('control_version')
                    if isinstance(version, bool) or not isinstance(version, int) or version <= command['expected_version']:
                        raise CommandRejected('Konfirmasi shutdown tidak valid.')
                    self.receipts.write(command_id, self.boot_id, 'executing')
                    self.commit(version, token)
                    self.shutdown()
                    submitted = True
                    self.receipts.write(command_id, self.boot_id, 'submitted')
                    self._retry(lambda: self.report(command_id, 'submitted'), deadline)
        except Exception as error:
            logging.warning('Remote %s command %s stopped (%s).', command['action'], command_id, type(error).__name__)
            if recorded:
                status = 'submitted' if submitted else 'cancelled' if isinstance(error, CommandRejected) else 'failed'
                try:
                    # Report fixed error categories, without configuration, URLs or captured contents.
                    detail = str(error) if isinstance(error, (CommandRejected, CaptureUnavailable)) else type(error).__name__
                    self.report(command_id, status, None if submitted else 'Perintah tidak dijalankan atau hasil tidak terkonfirmasi: ' + detail)
                except Exception:
                    pass
                try:
                    self.receipts.write(command_id, self.boot_id, status)
                except OSError:
                    pass
        finally:
            self.busy.clear()

    def run(self):
        while not self.stop.is_set():
            try:
                item = self.inbox.get(timeout=1)
            except queue.Empty:
                continue
            self.process(*item)

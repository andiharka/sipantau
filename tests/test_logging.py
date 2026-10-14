import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from client_logging import NetworkLog


class LoggingTests(unittest.TestCase):
    def test_network_storm_is_bounded_and_recovery_logged(self):
        now = [0]
        logs = NetworkLog(monotonic=lambda: now[0])
        with mock.patch.object(logging, 'warning') as warning, mock.patch.object(logging, 'info') as info:
            for _ in range(10000): logs.failure('heartbeat', Exception('URL-secret'))
            self.assertEqual(warning.call_count, 1)
            self.assertNotIn('URL-secret', str(warning.call_args))
            now[0] = 301
            logs.failure('heartbeat', Exception('changed text'))
            self.assertEqual(warning.call_count, 2)
            self.assertIn(9999, warning.call_args.args)
            logs.recovered('heartbeat')
            logs.recovered('heartbeat')
            info.assert_called_once()

    def test_log_rotation_retains_only_configured_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = RotatingFileHandler(Path(directory)/'agent.log', maxBytes=200, backupCount=5, encoding='utf-8')
            try:
                for i in range(1000): handler.emit(logging.LogRecord('test', logging.INFO, '', 0, 'x'*60, (), None))
            finally: handler.close()
            files = list(Path(directory).glob('agent.log*'))
            self.assertEqual(len(files), 6)
            self.assertTrue(all(p.stat().st_size <= 200 for p in files))

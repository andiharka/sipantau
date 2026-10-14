import unittest
from control_runtime import SessionClock


class ClockTests(unittest.TestCase):
    def test_countdown_uses_server_time_and_monotonic_elapsed(self):
        tick = [100.0]
        clock = SessionClock(lambda: tick[0])
        clock.sync(5, 10, '2026-10-14T10:01:00Z', '2026-10-14T10:00:00Z', 2)
        self.assertEqual(clock.remaining(), 59)
        tick[0] += 20
        self.assertEqual(clock.remaining(), 39)
        # Repeated stale packets cannot give the visitor more time.
        clock.sync(5, 10, '2026-10-14T10:01:00Z', '2026-10-14T10:00:00Z')
        self.assertEqual(clock.remaining(), 39)

    def test_new_session_replaces_deadline_and_unlimited_clears_it(self):
        clock = SessionClock(lambda: 100)
        clock.sync(1, 1, '2026-10-14T10:00:00Z', '2026-10-14T10:01:00Z')
        self.assertLess(clock.remaining(), 0)
        clock.sync(2, 2, '2026-10-14T10:10:00Z', '2026-10-14T10:00:00Z')
        self.assertEqual(clock.remaining(), 600)
        clock.sync(3, None, None, '2026-10-14T10:00:00Z')
        self.assertIsNone(clock.remaining())

    def test_rejects_timezone_free_timestamp(self):
        clock = SessionClock()
        with self.assertRaises(ValueError):
            clock.sync(1, 1, '2026-10-14T10:01:00', '2026-10-14T10:00:00Z')

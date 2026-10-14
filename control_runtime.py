"""Server-synchronized countdown independent of changes to the PC wall clock."""
import time
from datetime import datetime, timezone


def utc(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Server timestamps must include a timezone')
    return parsed


class SessionClock:
    def __init__(self, monotonic=time.monotonic):
        self.monotonic = monotonic
        self.deadline = None
        self.identity = None

    def clear(self):
        self.deadline = None
        self.identity = None

    def sync(self, version, session_id, expires_at, server_now, round_trip=0):
        if not expires_at:
            self.clear()
            return
        remaining = (utc(expires_at) - utc(server_now)).total_seconds() - max(0, round_trip) / 2
        identity = (version, session_id, expires_at)
        deadline = self.monotonic() + remaining
        # Repeating the same response must never extend a running session.
        if self.identity == identity and self.deadline is not None:
            deadline = min(deadline, self.deadline)
        self.identity = identity
        self.deadline = deadline

    def restore(self, version, session_id, expires_at, server_offset=0):
        if expires_at:
            from datetime import timedelta
            estimated_server_now = datetime.now(timezone.utc) + timedelta(seconds=server_offset)
            self.sync(version, session_id, expires_at, estimated_server_now.isoformat())
        else:
            self.clear()

    def remaining(self):
        return None if self.deadline is None else self.deadline - self.monotonic()

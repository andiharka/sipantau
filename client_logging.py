"""Bound repetitive network failures, without suppressing recovery events."""
import logging
import threading
import time


class NetworkLog:
    def __init__(self, interval=300, monotonic=time.monotonic):
        self.interval = interval
        self.monotonic = monotonic
        self.failures = {}
        self.lock = threading.Lock()

    def failure(self, service, error):
        now = self.monotonic()
        with self.lock:
            last, suppressed = self.failures.get(service, (float('-inf'), 0))
            if now - last < self.interval:
                self.failures[service] = (last, suppressed + 1)
                return
            self.failures[service] = (now, 0)
        # Log category only: requests exceptions can contain URLs with credentials.
        detail = str(error) if isinstance(error, str) and error.startswith('HTTP ') else type(error).__name__
        logging.warning('%s unavailable (%s); %s similar failures suppressed. Retrying in background.',
                        service, detail, suppressed)

    def recovered(self, service):
        with self.lock:
            previous = self.failures.pop(service, None)
        if previous is not None:
            logging.info('%s connection restored; %s similar failures suppressed.', service, previous[1])

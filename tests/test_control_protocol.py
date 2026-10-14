import copy
import logging
import unittest
from types import SimpleNamespace
from unittest import mock

with mock.patch.object(logging, "basicConfig"):
    import agent


class EndLoop(BaseException):
    pass


class Response:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self.data = data
        self.text = ""

    def json(self):
        return self.data


class ControlProtocolTests(unittest.TestCase):
    def setUp(self):
        self.original_state = copy.deepcopy(agent.local_state)
        self.original_clock = agent.SESSION_CLOCK
        agent.SESSION_CLOCK = agent.SessionClock()
        while not agent.GUI_COMMANDS.empty(): agent.GUI_COMMANDS.get_nowait()
        agent.local_state = {
            **copy.deepcopy(self.original_state),
            "is_locked": False,
            "from_when": "2026-09-28T00:00:00Z",
            "duration": 3600,
            "control_version": 3,
            "pending_actions": [{
                "id": "first",
                "action": "force-lock",
                "expected_version": 3,
            }],
        }

    def tearDown(self):
        agent.local_state = self.original_state
        agent.SESSION_CLOCK = self.original_clock

    def run_one_iteration(self, post):
        with (
            mock.patch.dict(agent.config, {"server_url": "http://server", "api_key": "test"}),
            mock.patch.object(agent.requests, "post", side_effect=post),
            mock.patch.object(agent, "save_state"),
            mock.patch.object(agent, "get_agent_hostname", return_value="PC-01"),
            mock.patch.object(agent, "get_local_ip", return_value="127.0.0.1"),
            mock.patch.object(agent, "get_running_apps", return_value=[]),
            mock.patch.object(agent, "root", SimpleNamespace(after=lambda *_: None)),
            mock.patch.object(agent.heartbeat_event, "wait", side_effect=EndLoop),
        ):
            with self.assertRaises(EndLoop):
                agent.heartbeat_loop()

    def test_stale_action_is_discarded_and_server_state_applied(self):
        def post(url, **kwargs):
            if url.endswith("/force-lock"):
                self.assertEqual(kwargs["json"]["expected_version"], 3)
                return Response(409, {"success": False})
            return Response(200, {
                "is_locked": False,
                "from_when": "2026-09-28T01:00:00Z",
                "duration": 7200,
                "control_version": 4,
                "server_now": "2026-09-28T01:00:00Z",
                "expires_at": None,
                "session_id": None,
            })

        self.run_one_iteration(post)
        self.assertEqual(agent.local_state["pending_actions"], [])
        self.assertFalse(agent.local_state["is_locked"])
        self.assertEqual(agent.local_state["duration"], 7200)
        self.assertEqual(agent.local_state["control_version"], 4)

    def test_successful_action_updates_version(self):
        def post(url, **kwargs):
            if url.endswith("/force-lock"):
                return Response(200, {"success": True, "control_version": 4})
            return Response(200, {
                "is_locked": True,
                "from_when": None,
                "duration": None,
                "control_version": 4,
                "server_now": "2026-09-28T01:00:00Z",
                "expires_at": None,
                "session_id": None,
            })

        self.run_one_iteration(post)
        self.assertEqual(agent.local_state["pending_actions"], [])
        self.assertTrue(agent.local_state["is_locked"])
        self.assertEqual(agent.local_state["control_version"], 4)

    def test_newer_local_action_survives_in_flight_request(self):
        def post(url, **kwargs):
            if url.endswith("/force-lock"):
                agent.set_local_control(False, "force-unlock")
                return Response(200, {"success": True, "control_version": 4})
            return Response(200, {
                "is_locked": True,
                "from_when": None,
                "duration": None,
                "control_version": 4,
                "server_now": "2026-09-28T01:00:00Z",
                "expires_at": None,
                "session_id": None,
            })

        self.run_one_iteration(post)
        pending = agent.local_state["pending_actions"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["action"], "force-unlock")
        self.assertEqual(pending[0]["expected_version"], 4)
        self.assertFalse(agent.local_state["is_locked"])


if __name__ == "__main__":
    unittest.main()

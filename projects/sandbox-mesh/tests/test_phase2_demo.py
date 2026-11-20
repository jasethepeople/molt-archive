from __future__ import annotations

import unittest

from mesh.phase2_demo import _verify_app_crash


class FakeClient:
    def __init__(self):
        self.items = {
            "0123456789ab": {"id": "0123456789ab", "status": "running", "agent_status": "alive"},
            "abcdef012345": {"id": "abcdef012345", "status": "queued", "agent_status": "waiting", "queue_position": 1},
        }
        self.resources = {"managed_containers": 2, "managed_networks": 2, "managed_volumes": 0}
        self.calls = []

    def call(self, method, path, payload=None):
        self.calls.append((method, path))
        if method == "GET" and path == "/api/resources":
            return dict(self.resources)
        if method == "GET" and path == "/api/queue":
            queued = [dict(item) for item in self.items.values() if item["status"] == "queued"]
            return {"items": queued}
        if method == "GET" and path.startswith("/api/sandboxes/"):
            identifier = path.rsplit("/", 1)[-1]
            return {"item": dict(self.items[identifier])}
        if method == "POST" and path.endswith("/crash-app"):
            identifier = path.split("/")[-2]
            self.items[identifier].update(status="failed", agent_status="dead")
            self.items["abcdef012345"].update(status="building", queue_position=None)
            self.resources["managed_containers"] -= 1
            self.resources["managed_networks"] -= 1
            return {"item": dict(self.items[identifier])}
        raise AssertionError(f"unexpected API call: {method} {path}")


class CrashAcceptanceTests(unittest.TestCase):
    def test_app_crash_cleans_one_sandbox_and_advances_fifo_queue(self):
        client = FakeClient()
        submitted = [
            ("app crash", "0123456789ab", "buildpack"),
            ("queued app", "abcdef012345", "buildpack"),
        ]
        _verify_app_crash(client, submitted, "0123456789ab")
        self.assertEqual(client.items["0123456789ab"]["status"], "failed")
        self.assertEqual(client.items["0123456789ab"]["agent_status"], "dead")
        self.assertEqual(client.items["abcdef012345"]["status"], "building")
        self.assertEqual(client.resources, {"managed_containers": 1, "managed_networks": 1, "managed_volumes": 0})
        self.assertIn(("POST", "/api/sandboxes/0123456789ab/crash-app"), client.calls)


if __name__ == "__main__":
    unittest.main()

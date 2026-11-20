from __future__ import annotations

import pathlib
import unittest

from mesh.demo import DemoError, create_repository, exercise_repository


ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeClient:
    """Minimal stand-in for mesh.demo.Client: scripted .call responses."""

    def __init__(self, item: dict):
        self.item = dict(item)
        self.calls: list[tuple[str, str]] = []

    def call(self, method: str, path: str, payload: dict | None = None) -> dict:
        self.calls.append((method, path))
        if method == "POST" and path == "/api/sandboxes":
            return {"item": {"id": "a" * 12}}
        if method == "GET" and path == "/api/sandboxes/" + "a" * 12:
            return {"item": dict(self.item)}
        if method == "POST" and path == "/api/sandboxes/" + "a" * 12 + "/destroy":
            self.item["status"] = "destroyed"
            return {"item": {"id": "a" * 12, "status": "destroyed"}}
        raise AssertionError(f"unexpected call {method} {path}")


class DemoRobustnessTests(unittest.TestCase):
    def test_create_repository_raises_on_failed_status(self):
        client = FakeClient({"id": "a" * 12, "status": "failed",
                             "error": "isolated build step exceeded its 900-second timeout"})
        with self.assertRaises(DemoError) as ctx:
            create_repository(client, "https://github.com/heroku/python-getting-started")
        self.assertIn("900-second timeout", str(ctx.exception))

    def test_exercise_repository_records_failed_build_and_returns_none(self):
        client = FakeClient({"id": "a" * 12, "status": "failed",
                             "error": "isolated build step exceeded its 900-second timeout"})
        failures: list[str] = []
        result = exercise_repository(client, failures, "Python",
                                     "https://github.com/heroku/python-getting-started",
                                     "buildpack")
        self.assertIsNone(result)
        self.assertEqual(len(failures), 1)
        self.assertIn("Python", failures[0])

    def test_exercise_repository_build_type_mismatch_is_recorded_not_raised(self):
        client = FakeClient({"id": "a" * 12, "status": "running", "build_type": "dockerfile"})
        failures: list[str] = []
        result = exercise_repository(client, failures, "Python",
                                     "https://github.com/heroku/python-getting-started",
                                     "buildpack")
        self.assertIsNone(result)
        self.assertEqual(len(failures), 1)
        self.assertIn("dockerfile", failures[0])
        self.assertIn(("POST", "/api/sandboxes/" + "a" * 12 + "/destroy"), client.calls)

    def test_failure_cleanup_waits_for_async_cleanup_before_comparing(self):
        from unittest import mock
        import mesh.demo as demo_module
        baseline = {"containers": ["warm"], "networks": [], "volumes": []}
        with_extra = {"containers": ["warm", "sm-edge-" + "b" * 12], "networks": [], "volumes": []}
        snapshots = [baseline, with_extra, baseline]
        class FakeDriver:
            def __init__(self, root, config):
                self.calls = 0
            def snapshot_all(self):
                self.calls += 1
                return snapshots[min(self.calls - 1, len(snapshots) - 1)]
        fake_driver = FakeDriver(None, None)
        class FixtureClient:
            def call(self, method: str, path: str, payload: dict | None = None) -> dict:
                if method == "POST" and path == "/api/demo/build-failure":
                    return {"item": {"id": "b" * 12}}
                if method == "GET" and path == "/api/sandboxes/" + "b" * 12:
                    return {"item": {"id": "b" * 12, "status": "failed"}}
                if method == "POST" and path == "/api/sandboxes/" + "b" * 12 + "/destroy":
                    return {"item": {"id": "b" * 12, "status": "destroyed"}}
                raise AssertionError(f"unexpected call {method} {path}")
        failures: list[str] = []
        with mock.patch.object(demo_module, "DockerDriver", return_value=fake_driver):
            demo_module.failure_cleanup(FixtureClient(), failures)
        self.assertEqual(failures, [])
        self.assertEqual(fake_driver.calls, 3)


if __name__ == "__main__":
    unittest.main()

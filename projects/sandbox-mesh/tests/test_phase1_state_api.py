from __future__ import annotations

import email.message
import hashlib
import io
import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from mesh.config import load_config
from mesh.orchestrator import Orchestrator
from mesh.server import APIApplication, AuthManager
from mesh.state import CapacityError, StateError, StateStore
from mesh.driver import DriverError
from mesh.git_source import GitSource


ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP_ROOT = ROOT / "tmp"
TMP_ROOT.mkdir(exist_ok=True)
TOKEN = "phase1-test-bearer-token-0123456789abcdef"


def headers(**values):
    result = email.message.Message()
    for key, value in values.items():
        result[key.replace("_", "-")] = value
    return result


class StateStoreTests(unittest.TestCase):
    def test_state_persists_and_enforces_single_active_job(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
            database = pathlib.Path(temporary) / "state.sqlite3"
            first = StateStore(database, concurrency_cap=1)
            record = first.create("https://github.com/example/repo.git", {"mode": "test"})
            first.update(record["id"], status="cloning", run_command=["python", "app.py"])
            second = StateStore(database, concurrency_cap=1)
            restored = second.get(record["id"])
            self.assertEqual(restored["status"], "cloning")
            self.assertEqual(restored["run_command"], ["python", "app.py"])
            self.assertEqual(restored["metadata"], {"mode": "test"})
            next_record = second.create("https://github.com/example/other.git")
            self.assertEqual(next_record["status"], "queued")
            self.assertEqual(second.queue_position(next_record["id"]), 1)
            self.assertIsNone(second.claim_next())
            with self.assertRaises(StateError):
                second.update(record["id"], status="running")
            second.update(record["id"], status="failed", error="expected")
            claimed = second.claim_next()
            self.assertEqual(claimed["id"], next_record["id"])
            self.assertEqual(claimed["status"], "cloning")
            self.assertEqual(database.stat().st_mode & 0o777, 0o600)


class FakeQueueStore:
    concurrency_cap = 2

    def __init__(self, record):
        self.record = record

    def queued(self):
        return [self.record]

    def active_count(self):
        return 1


class FakeOrchestrator:
    def __init__(self):
        self.record = {
            "id": "abcdef123456",
            "url": "https://github.com/example/repo.git",
            "commit_sha": "a" * 40,
            "status": "running",
            "created_at": 1.0,
            "updated_at": 2.0,
            "expires_at": 1801.0,
            "build_kind": "buildpack",
            "app_port": 8080,
            "proxy_url": f"/s/abcdef123456/{'A' * 43}/",
            "proxy_ticket_hash": hashlib.sha256(b"A" * 43).hexdigest(),
            "error": None,
            "agent_status": "alive",
            "agent_last_heartbeat": 3.0,
        }
        self.queued_record = {**self.record, "id": "fedcba654321", "status": "queued", "queue_reason": "waiting for scheduler capacity", "agent_status": "waiting", "agent_last_heartbeat": None}
        self.store = FakeQueueStore(self.queued_record)
        self.calls = []

    def list(self):
        return [self.record, self.queued_record]

    def get(self, identifier):
        return {self.record["id"]: self.record, self.queued_record["id"]: self.queued_record}.get(identifier)

    def queue_position(self, identifier):
        return 1 if identifier == self.queued_record["id"] else None

    def scheduler_snapshot(self):
        return {"host_id": "test", "concurrency_cap": 2, "active": 1, "queued": 1, "guard_allowed": True}

    def resource_snapshot(self):
        return {"managed_containers": 2, "managed_networks": 2, "managed_volumes": 0}

    def cancel(self, identifier):
        self.calls.append(("cancel", identifier))
        return {**self.queued_record, "status": "destroyed"}

    def stats(self, identifier):
        return {"cpu": "1%", "memory": "4MiB / 2GiB", "pids": "2"}

    def submit(self, url):
        self.calls.append(("run", url))
        return {**self.record, "url": url, "status": "queued"}

    def submit_demo(self, name):
        self.calls.append(("demo", name))
        return {**self.record, "url": f"demo://{name}", "status": "queued"}

    def stop(self, identifier):
        self.calls.append(("stop", identifier))
        return {**self.record, "status": "stopped"}

    def restart(self, identifier):
        self.calls.append(("restart", identifier))
        return {**self.record, "status": "queued"}

    def destroy(self, identifier):
        self.calls.append(("destroy", identifier))
        return {**self.record, "status": "destroyed"}

    def crash_app(self, identifier):
        self.calls.append(("crash_app", identifier))
        return self.record

    def kill_agent(self, identifier):
        self.calls.append(("kill_agent", identifier))
        return self.record

    def logs(self, identifier):
        self.calls.append(("logs", identifier))
        return "sample log\n"

    def proxy_target(self, identifier):
        return "127.0.0.1", 8900


class FakeHandler:
    def __init__(self):
        self.headers = headers(Cookie="sm_session=session", Authorization="Bearer " + TOKEN, X_Forwarded_Host="attacker")
        self.rfile = io.BytesIO()
        self.wfile = io.BytesIO()
        self.out_headers = []
        self.status = None
        self.close_connection = False

    def send_response(self, status, reason=""):
        self.status = status

    def send_header(self, key, value):
        self.out_headers.append((key, value))

    def end_headers(self):
        return None

    def send_error(self, status, message=""):
        self.status = status
        self.error = message


class APIAuthTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT)
        self.orchestrator = FakeOrchestrator()
        self.auth = AuthManager(TOKEN, 3600)
        self.app = APIApplication(ROOT, self.config, self.orchestrator, self.auth)
        self.session_id, self.csrf = self.auth.create()
        self.session_headers = headers(Cookie=f"sm_session={self.session_id}", X_CSRF_Token=self.csrf)

    def test_api_requires_login_and_csrf(self):
        response = self.app.dispatch("GET", "/api/sandboxes", headers(), b"")
        self.assertEqual(response.status, 401)
        response = self.app.dispatch("POST", "/api/logout", headers(Cookie=f"sm_session={self.session_id}"), b"")
        self.assertEqual(response.status, 403)
        response = self.app.dispatch("POST", "/api/logout", self.session_headers, b"")
        self.assertEqual(response.status, 200)
        self.assertFalse(self.auth.session_state(self.session_headers)["authenticated"])

    def test_run_stop_restart_destroy_and_logs_endpoints(self):
        run = self.app.dispatch("POST", "/api/sandboxes", self.session_headers, json.dumps({"url": "https://github.com/example/repo"}).encode())
        self.assertEqual(run.status, 202)
        self.assertEqual(self.orchestrator.calls[-1], ("run", "https://github.com/example/repo"))
        listed = self.app.dispatch("GET", "/api/sandboxes", self.session_headers, b"")
        self.assertEqual(listed.status, 200)
        self.assertEqual(json.loads(listed.body)["items"][0]["stats"]["pids"], "2")
        for operation, expected in (("stop", "stopped"), ("restart", "queued"), ("destroy", "destroyed")):
            result = self.app.dispatch("POST", f"/api/sandboxes/abcdef123456/{operation}", self.session_headers, b"")
            self.assertEqual(result.status, 200)
            self.assertEqual(json.loads(result.body)["item"]["status"], expected)
        logs = self.app.dispatch("GET", "/api/sandboxes/abcdef123456/logs", self.session_headers, b"")
        self.assertEqual(json.loads(logs.body)["logs"], "sample log\n")
        self.assertIn(("stop", "abcdef123456"), self.orchestrator.calls)
        self.assertIn(("restart", "abcdef123456"), self.orchestrator.calls)
        self.assertIn(("destroy", "abcdef123456"), self.orchestrator.calls)

    def test_queue_scheduler_health_and_agent_fields_are_publicly_shaped(self):
        listed = self.app.dispatch("GET", "/api/sandboxes", self.session_headers, b"")
        items = json.loads(listed.body)["items"]
        self.assertEqual(items[0]["agent_status"], "alive")
        self.assertEqual(items[0]["agent_last_heartbeat"], 3.0)
        self.assertEqual(items[1]["status"], "queued")
        self.assertEqual(items[1]["queue_position"], 1)

        queue = self.app.dispatch("GET", "/api/queue", self.session_headers, b"")
        self.assertEqual(queue.status, 200)
        queued_items = json.loads(queue.body)["items"]
        self.assertEqual(queued_items[0]["id"], "fedcba654321")
        self.assertEqual(queued_items[0]["queue_reason"], "waiting for scheduler capacity")
        self.assertEqual(queued_items[0]["queue_position"], 1)

        scheduler = self.app.dispatch("GET", "/api/scheduler", self.session_headers, b"")
        self.assertEqual(json.loads(scheduler.body)["concurrency_cap"], 2)
        health = self.app.dispatch("GET", "/api/health", self.session_headers, b"")
        self.assertEqual(json.loads(health.body)["queued"], 1)

        cancelled = self.app.dispatch("POST", "/api/sandboxes/fedcba654321/cancel", self.session_headers, b"")
        self.assertEqual(cancelled.status, 200)
        self.assertEqual(json.loads(cancelled.body)["item"]["status"], "destroyed")
        self.assertIn(("cancel", "fedcba654321"), self.orchestrator.calls)

    def test_resource_counts_and_supervisor_test_actions_are_authenticated(self):
        resources = self.app.dispatch("GET", "/api/resources", self.session_headers, b"")
        self.assertEqual(resources.status, 200)
        self.assertEqual(json.loads(resources.body)["managed_containers"], 2)
        for action, method_name in (("crash-app", "crash_app"), ("kill-agent", "kill_agent")):
            result = self.app.dispatch("POST", f"/api/sandboxes/abcdef123456/{action}", self.session_headers, b"")
            self.assertEqual(result.status, 200)
            self.assertIn((method_name, "abcdef123456"), self.orchestrator.calls)
        unauthorized = self.app.dispatch("GET", "/api/resources", headers(), b"")
        self.assertEqual(unauthorized.status, 401)

    def test_login_is_bearer_only_and_does_not_expose_token(self):
        invalid = self.app.dispatch("POST", "/api/login", headers(Authorization="Bearer wrong"), b"{}")
        self.assertEqual(invalid.status, 401)
        valid = self.app.dispatch("POST", "/api/login", headers(Authorization="Bearer " + TOKEN), b"{}")
        self.assertEqual(valid.status, 200)
        self.assertNotIn(TOKEN.encode(), valid.body)
        self.assertIn("Set-Cookie", valid.headers)

    def test_proxy_strips_credentials_and_streams_content(self):
        handler = FakeHandler()
        fake_response = mock.Mock()
        fake_response.status = 200
        fake_response.reason = "OK"
        fake_response.getheaders.return_value = [("Content-Type", "text/plain"), ("Content-Length", "5"), ("Set-Cookie", "app=1; Path=/"), ("Location", "/next")]
        fake_response.getheader.return_value = "text/plain"
        fake_response.read1.side_effect = [b"hello", b""]
        fake_connection = mock.Mock()
        fake_connection.getresponse.return_value = fake_response
        ticket = "A" * 43
        with mock.patch("mesh.server.http.client.HTTPConnection", return_value=fake_connection):
            self.app.proxy(handler, "GET", f"/s/abcdef123456/{ticket}/?q=1")
        call = fake_connection.request.call_args
        forwarded = call.kwargs["headers"]
        self.assertNotIn("Authorization", forwarded)
        self.assertNotIn("Cookie", forwarded)
        self.assertNotIn("X-Forwarded-Host", forwarded)
        self.assertEqual(forwarded["Host"], "127.0.0.1:8900")
        self.assertEqual(forwarded["X-Forwarded-Prefix"], f"/s/abcdef123456/{ticket}")
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.wfile.getvalue(), b"hello")
        output_headers = dict(handler.out_headers)
        self.assertEqual(output_headers["Location"], f"/s/abcdef123456/{ticket}/next")
        self.assertNotIn("Set-Cookie", output_headers)
        self.assertEqual(output_headers["Access-Control-Allow-Origin"], "null")


class OrchestratorTests(unittest.TestCase):
    class Driver:
        def __init__(self):
            self.destroyed = []
            self.running = set()
            self.fail_build = False
            self.fail_cleanup = False
            self.released = []

        def ensure_ready(self):
            return {"engine": "docker"}

        def prepare_network(self, record, plan, on_output):
            return f"sm-net-{record['id']}", f"sm-edge-{record['id']}", 8900

        def build(self, record, repo_path, plan, network_name, on_output):
            if self.fail_build:
                raise DriverError("controlled build failure")
            return "sandbox-mesh/test:sha"

        def release_build_network(self, network):
            self.released.append(network)

        def start(self, record, image, plan, network_name, on_output):
            container = f"container-{record['id']}"
            self.running.add(container)
            return {"container_id": container, "container_name": container, "host_port": 8900, "image": image}

        def stop(self, record):
            self.running.discard(record.get("container_id"))
            self.destroy(record)

        def destroy(self, record):
            if self.fail_cleanup:
                raise DriverError("controlled cleanup failure")
            self.destroyed.append(record["id"])
            self.running.discard(record.get("container_id"))

        def logs(self, record, tail=200):
            return "runtime marker\n"

        def follow_logs(self, record, emit, stopped):
            emit("runtime marker")
            stopped.wait(0.05)

        def stats(self, record):
            return {"cpu": "0%"}

        def proxy_target(self, record):
            return "127.0.0.1", 8900

        def container_exists(self, record):
            return record.get("container_id") in self.running

        def reconcile(self, active_ids):
            return []

        def ensure_warm_pool(self):
            return None

        def snapshot_all(self):
            return {"containers": [], "volumes": [], "networks": []}

    class SafeOrchestrator(Orchestrator):
        def _wait_healthy(self, host_port):
            return None

    def wait_status(self, orchestrator, identifier, status):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = orchestrator.get(identifier)
            if record and record["status"] == status:
                return record
            if record and record["status"] == "failed" and status != "failed":
                self.fail(record.get("error"))
            time.sleep(0.02)
        self.fail(f"sandbox did not reach {status}")

    def test_run_logs_proxy_target_stop_restart_destroy(self):
        config = load_config(ROOT)
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
            root = pathlib.Path(temporary)
            source_root = root / "sandbox-mesh"
            source_root.mkdir()
            (source_root / "config.yaml").write_text((ROOT / "config.yaml").read_text())
            import shutil
            shutil.copytree(ROOT / "samples", source_root / "samples")
            store = StateStore(root / "state.sqlite3", 1)
            driver = self.Driver()
            source = GitSource(source_root, config)
            orchestrator = self.SafeOrchestrator(source_root, config, store, source, driver)
            result = orchestrator.submit_demo("node-basic")
            record = self.wait_status(orchestrator, result["id"], "running")
            self.assertIn("runtime marker", orchestrator.logs(record["id"]))
            self.assertEqual(orchestrator.proxy_target(record["id"]), ("127.0.0.1", 8900))
            stopped = orchestrator.stop(record["id"])
            self.assertEqual(stopped["status"], "stopped")
            restarted = orchestrator.restart(record["id"])
            self.wait_status(orchestrator, restarted["id"], "running")
            destroyed = orchestrator.destroy(record["id"])
            self.assertEqual(destroyed["status"], "destroyed")
            self.assertTrue(driver.released)
            orchestrator.stop_background()

    def test_failed_build_cleans_the_job_resources(self):
        config = load_config(ROOT)
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
            root = pathlib.Path(temporary)
            source_root = root / "sandbox-mesh"
            source_root.mkdir()
            (source_root / "config.yaml").write_text((ROOT / "config.yaml").read_text())
            import shutil
            shutil.copytree(ROOT / "samples", source_root / "samples")
            store = StateStore(root / "state.sqlite3", 1)
            driver = self.Driver()
            driver.fail_build = True
            driver.fail_cleanup = True
            source = GitSource(source_root, config)
            orchestrator = self.SafeOrchestrator(source_root, config, store, source, driver)
            result = orchestrator.submit_demo("node-basic")
            failed = self.wait_status(orchestrator, result["id"], "failed")
            self.assertIn("controlled build failure", failed["error"])
            self.assertIsNotNone(failed["network_name"])
            blocked = orchestrator.submit_demo("node-basic")
            self.assertEqual(blocked["status"], "queued")
            self.assertEqual(store.queue_position(blocked["id"]), 1)
            self.assertIsNone(store.claim_next())
            driver.fail_cleanup = False
            destroyed = orchestrator.destroy(result["id"])
            self.assertEqual(destroyed["status"], "destroyed")
            self.assertIn(result["id"], driver.destroyed)
            resumed = self.wait_status(orchestrator, blocked["id"], "failed")
            self.assertIn("controlled build failure", resumed["error"])
            orchestrator.destroy(blocked["id"])
            orchestrator.stop_background()


if __name__ == "__main__":
    unittest.main()

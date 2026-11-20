from __future__ import annotations

import json
import pathlib
import sqlite3
import tempfile
import time
import unittest

from mesh.config import load_config
from mesh.host_guard import HostGuard, HostDecision
from mesh.orchestrator import Orchestrator
from mesh.state import StateStore


ROOT = pathlib.Path(__file__).resolve().parents[1]


class HostGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temporary.name)
        self.loadavg = root / "loadavg"
        self.meminfo = root / "meminfo"
        self.config = {"max_1m_load_per_cpu": 1.0, "min_available_memory_bytes": 2_000_000_000}
        self.guard = HostGuard(self.config, 2, self.loadavg, self.meminfo)

    def tearDown(self):
        self.temporary.cleanup()

    def write_measurements(self, load: float, memory: int):
        self.loadavg.write_text(f"{load} 0.00 0.00 1/1 1\n", encoding="ascii")
        self.meminfo.write_text(f"MemAvailable: {memory // 1024} kB\n", encoding="ascii")

    def test_load_and_memory_thresholds_allow_or_hold(self):
        self.write_measurements(1.99, 2_100_000_000)
        self.assertTrue(self.guard.evaluate().allowed)
        self.write_measurements(2.01, 3_000_000_000)
        self.assertEqual(self.guard.evaluate().reason, "waiting for host capacity")
        self.write_measurements(1.00, 1_999_000_000)
        self.assertFalse(self.guard.evaluate().allowed)

    def test_missing_or_invalid_measurements_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "load average"):
            self.guard.evaluate()
        self.write_measurements(-1, 3_000_000_000)
        with self.assertRaises(RuntimeError):
            self.guard.evaluate()


class QueueStateTests(unittest.TestCase):
    def test_fifo_queue_claim_respects_cap_and_survives_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "state.sqlite3"
            store = StateStore(path, 2)
            first = store.create("demo://one")
            second = store.create("demo://two")
            third = store.create("demo://three")
            self.assertEqual([store.queue_position(item["id"]) for item in (first, second, third)], [1, 2, 3])
            claimed_first = store.claim_next()
            claimed_second = store.claim_next()
            self.assertEqual([claimed_first["id"], claimed_second["id"]], [first["id"], second["id"]])
            self.assertIsNone(store.claim_next())
            reopened = StateStore(path, 2)
            self.assertEqual(reopened.get(third["id"])["status"], "queued")
            self.assertEqual(reopened.queue_position(third["id"]), 1)
            reopened.update(first["id"], status="failed")
            self.assertEqual(reopened.claim_next()["id"], third["id"])

    def test_legacy_database_migrates_without_losing_queued_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "legacy.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE sandboxes(id TEXT PRIMARY KEY,url TEXT NOT NULL,status TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,metadata TEXT NOT NULL DEFAULT '{}')")
                db.execute("INSERT INTO sandboxes(id,url,status,created_at,updated_at,metadata) VALUES('0123456789ab','demo://legacy','queued',10,10,'{}')")
            store = StateStore(path, 1)
            migrated = store.get("0123456789ab")
            self.assertEqual(migrated["status"], "queued")
            self.assertEqual(store.queue_position(migrated["id"]), 1)
            self.assertEqual(migrated["agent_status"], "waiting")


class FakeGuard:
    def __init__(self, allowed: bool):
        self.allowed = allowed
        self.calls = 0

    def evaluate(self):
        self.calls += 1
        return HostDecision(self.allowed, None if self.allowed else "waiting for host capacity", 0.0, 2.0, 8_000_000_000, 2_000_000_000)


class FakeDriver:
    def __init__(self, running=None):
        self.running = running or {}
        self.destroyed = []
        self.ready_calls = 0
        self.warm_calls = 0
        self.reconciled = None
        self.stop_calls = 0
        self.killed = []
        self.killed = []


    def ensure_ready(self):
        self.ready_calls += 1
        return {"engine": "fake"}

    def ensure_warm_pool(self):
        self.warm_calls += 1

    def reconcile(self, active):
        self.reconciled = set(active)
        return []

    def container_exists(self, record):
        return self.running.get(record["id"], True)

    def destroy(self, record):
        self.destroyed.append(record["id"])

    def kill_process(self, record, role):
        self.killed.append((record["id"], role))
        self.running[record["id"]] = False

    def snapshot_all(self):
        return {
            "containers": [identifier for identifier, running in self.running.items() if running],
            "networks": [identifier for identifier, running in self.running.items() if running],
            "volumes": [],
        }

    def stop(self, record):
        self.stop_calls += 1


class FakeSource:
    fixture_names = {"node-basic", "python-basic", "dockerfile-basic"}

    def normalize(self, value):
        return value


class RecordingOrchestrator(Orchestrator):
    def _spawn(self, record):
        self.spawned.append(record["id"])
        return record


class SchedulerTests(unittest.TestCase):
    def make_orchestrator(self, temporary, cap=3, allowed=True, driver=None):
        root = pathlib.Path(temporary)
        (root / "logs").mkdir(exist_ok=True)
        config = load_config(ROOT)
        config["runtime"]["host_id"] = "test"
        config["scheduler"]["host_guard"]["retry_interval_seconds"] = 0.2
        store = StateStore(root / "state.sqlite3", cap)
        driver = driver or FakeDriver()
        orchestrator = RecordingOrchestrator(root, config, store, FakeSource(), driver, FakeGuard(allowed))
        orchestrator.spawned = []
        return orchestrator, store, driver

    def test_three_submissions_start_up_to_cap_and_preserve_fifo_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            orchestrator, store, driver = self.make_orchestrator(temporary, cap=3)
            records = [orchestrator.submit_demo(name) for name in ("node-basic", "python-basic", "dockerfile-basic")]
            self.assertEqual(len(orchestrator.spawned), 3)
            self.assertEqual(store.active_count(), 3)
            self.assertEqual([store.get(item["id"])["status"] for item in records], ["cloning"] * 3)
            orchestrator.stop_background()

    def test_cap_one_queues_later_jobs_and_stopping_queued_avoids_driver(self):
        with tempfile.TemporaryDirectory() as temporary:
            orchestrator, store, driver = self.make_orchestrator(temporary, cap=1)
            first = orchestrator.submit_demo("node-basic")
            second = orchestrator.submit_demo("python-basic")
            third = orchestrator.submit_demo("dockerfile-basic")
            self.assertEqual(store.get(first["id"])["status"], "cloning")
            self.assertEqual([store.queue_position(second["id"]), store.queue_position(third["id"])], [1, 2])
            self.assertEqual(store.get(second["id"])["queue_reason"], "waiting for scheduler capacity")
            stopped = orchestrator.stop(third["id"])
            self.assertEqual(stopped["status"], "stopped")
            self.assertEqual(driver.stop_calls, 0)
            self.assertEqual(len(orchestrator.spawned), 1)
            orchestrator.stop_background()

    def test_host_guard_holds_work_and_scheduler_retries_fifo_when_clear(self):
        with tempfile.TemporaryDirectory() as temporary:
            orchestrator, store, _ = self.make_orchestrator(temporary, cap=3, allowed=False)
            first = orchestrator.submit_demo("node-basic")
            second = orchestrator.submit_demo("python-basic")
            self.assertEqual(first["status"], "queued")
            self.assertEqual(store.get(first["id"])["queue_reason"], "waiting for host capacity")
            orchestrator.host_guard.allowed = True
            orchestrator._schedule()
            self.assertEqual(orchestrator.spawned, [first["id"], second["id"]])
            orchestrator.stop_background()

    def test_startup_reconcile_keeps_queue_and_does_not_start_when_guard_holds(self):
        with tempfile.TemporaryDirectory() as temporary:
            orchestrator, store, driver = self.make_orchestrator(temporary, cap=1, allowed=False)
            queued = store.create("demo://queued")
            orchestrator.startup_reconcile()
            self.assertEqual(store.get(queued["id"])["status"], "queued")
            self.assertEqual(driver.reconciled, set())
            self.assertEqual(orchestrator.spawned, [])
            self.assertEqual(driver.warm_calls, 1)
            orchestrator.stop_background()

    def test_crashed_container_cleanup_is_isolated_to_one_sandbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            driver = FakeDriver()
            orchestrator, store, _ = self.make_orchestrator(temporary, cap=3, driver=driver)
            identifiers = []
            for index in range(3):
                record = store.create(f"demo://crash-{index}")
                identifiers.append(record["id"])
                for status in ("cloning", "detecting", "building", "starting", "running"):
                    record = store.update(record["id"], status=status)
                store.update(record["id"], container_id="container-"+record["id"], container_name="sm-app-"+record["id"], network_name="sm-net-"+record["id"], egress_id="sm-edge-"+record["id"], agent_status="alive")
            driver.running = {identifiers[0]: False, identifiers[1]: True, identifiers[2]: True}
            orchestrator.reap_expired()
            self.assertEqual(store.get(identifiers[0])["status"], "failed")
            self.assertEqual([store.get(value)["status"] for value in identifiers[1:]], ["running", "running"])
            self.assertEqual(driver.destroyed, [identifiers[0]])
            orchestrator.stop_background()

    def test_crash_action_kills_only_selected_app_and_cleans_its_resources(self):
        with tempfile.TemporaryDirectory() as temporary:
            driver = FakeDriver()
            orchestrator, store, _ = self.make_orchestrator(temporary, cap=2, driver=driver)
            identifiers = []
            for index in range(2):
                record = store.create(f"demo://kill-{index}")
                identifiers.append(record["id"])
                record = store.claim_next()
                for status in ("detecting", "building", "starting", "running"):
                    record = store.update(record["id"], status=status)
                store.update(record["id"], container_id=f"container-{record['id']}", container_name=f"sm-app-{record['id']}", network_name=f"sm-net-{record['id']}", egress_id=f"sm-edge-{record['id']}", agent_status="alive")
                driver.running[record["id"]] = True
            before = orchestrator.resource_snapshot()
            result = orchestrator.crash_app(identifiers[0])
            self.assertEqual(result["status"], "running")
            self.assertEqual(driver.killed, [(identifiers[0], "app")])
            self.assertEqual(store.get(identifiers[1])["status"], "running")
            orchestrator.reap_expired()
            self.assertEqual(store.get(identifiers[0])["status"], "failed")
            self.assertEqual(store.get(identifiers[0])["agent_status"], "dead")
            self.assertEqual(store.get(identifiers[1])["status"], "running")
            after = orchestrator.resource_snapshot()
            self.assertEqual(after["managed_containers"], before["managed_containers"] - 1)
            self.assertEqual(after["managed_networks"], before["managed_networks"] - 1)
            self.assertEqual(driver.destroyed, [identifiers[0]])
            orchestrator.stop_background()

    def test_app_crash_action_is_reaped_and_managed_resources_return_to_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            driver = FakeDriver()
            orchestrator, store, _ = self.make_orchestrator(temporary, cap=1, driver=driver)
            record = store.create("demo://crash-action")
            record = store.claim_next()
            for status in ("detecting", "building", "starting", "running"):
                record = store.update(record["id"], status=status)
            record = store.update(record["id"], container_id="container-" + record["id"], container_name="sm-app-" + record["id"], network_name="sm-net-" + record["id"], egress_id="sm-edge-" + record["id"], agent_status="alive")
            driver.running[record["id"]] = True
            before = orchestrator.resource_snapshot()
            self.assertEqual(before, {"managed_containers": 1, "managed_networks": 1, "managed_volumes": 0})

            result = orchestrator.crash_app(record["id"])
            self.assertEqual(result["status"], "running")
            self.assertEqual(driver.killed, [(record["id"], "app")])
            orchestrator.reap_expired()

            failed = store.get(record["id"])
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["agent_status"], "dead")
            self.assertIsNone(failed["container_id"])
            self.assertEqual(orchestrator.resource_snapshot(), {"managed_containers": 0, "managed_networks": 0, "managed_volumes": 0})
            self.assertEqual(driver.destroyed, [record["id"]])
            orchestrator.stop_background()

    def test_heartbeat_updates_agent_state_and_silence_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            orchestrator, store, driver = self.make_orchestrator(temporary, cap=1)
            record = store.create("demo://agent")
            record = store.claim_next()
            for status in ("detecting", "building", "starting", "running"):
                record = store.update(record["id"], status=status)
            now = time.time()
            store.update(record["id"], agent_started_at=now, agent_status="starting")
            orchestrator._observe_log_line(record["id"], f"agent-heartbeat {record['id']} {int(now)}")
            self.assertEqual(store.get(record["id"])["agent_status"], "alive")
            store.update(record["id"], agent_last_heartbeat=now - 20)
            orchestrator.reap_expired()
            self.assertEqual(store.get(record["id"])["agent_status"], "silent")
            self.assertEqual(store.get(record["id"])["status"], "running")
            orchestrator.stop_background()


if __name__ == "__main__":
    unittest.main()

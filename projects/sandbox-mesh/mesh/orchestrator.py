from __future__ import annotations

import hashlib
import pathlib
import re
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.request

from .config import ConfigError
from .detector import DetectionError, detect
from .driver import DriverError
from .git_source import GitSource
from .host_guard import HostGuard
from .repository import RepositoryError
from .state import CapacityError, StateError, StateStore


ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")


class Orchestrator:
    def __init__(self, project_root: pathlib.Path, config: dict, store: StateStore, source: GitSource, driver, host_guard=None):
        self.root = project_root.resolve()
        self.config = config
        self.store = store
        self.source = source
        self.driver = driver
        self.host_guard = host_guard or HostGuard(config["scheduler"]["host_guard"], config["runtime"]["cpu_count"])
        self.logs_root = self.root / "logs"
        self.logs_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.tmp_root = self.root / "tmp"
        self.tmp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._jobs: dict[str, threading.Thread] = {}
        self._tailers: dict[str, tuple[threading.Event, threading.Thread]] = {}
        self._stop = threading.Event()
        self._background: threading.Thread | None = None
        self._scheduler_lock = threading.RLock()
        self._last_reap = 0.0

    def _log(self, identifier: str, text: str) -> None:
        if not ID_PATTERN.fullmatch(identifier):
            return
        path = self.logs_root / f"{identifier}.log"
        safe = str(text).replace("\x00", "").replace("\r", "")
        if not safe.endswith("\n"):
            safe += "\n"
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with path.open("a", encoding="utf-8", errors="replace") as output:
                output.write(safe)
                output.flush()
            try:
                if path.stat().st_size > 4_194_304:
                    content = path.read_bytes()[-2_097_152:]
                    temporary = self.tmp_root / f"{identifier}.log.tmp"
                    temporary.write_bytes(content)
                    temporary.chmod(0o600)
                    temporary.replace(path)
            except OSError:
                pass


    def _spawn(self, record: dict) -> dict:
        identifier = record["id"]
        thread = threading.Thread(target=self._run_job, args=(identifier,), name=f"sandbox-{identifier}", daemon=True)
        with self._lock:
            self._jobs[identifier] = thread
        thread.start()
        return record

    def _observe_log_line(self, identifier: str, line: str) -> None:
        self._log(identifier, line)
        match = re.search(r"(?:^|\s)agent-heartbeat ([0-9a-f]{12}) ([0-9]{1,12})(?:\s|$)", line)
        if not match or match.group(1) != identifier:
            return
        timestamp = int(match.group(2))
        now = time.time()
        if not 0 <= timestamp <= now + 30:
            return
        record = self.store.get(identifier)
        if record and record["status"] in {"starting", "running"}:
            previous = record.get("agent_last_heartbeat")
            if previous is not None and timestamp < float(previous):
                return
            self.store.update(
                identifier,
                agent_status="alive",
                agent_started_at=record.get("agent_started_at") or now,
                agent_last_heartbeat=float(timestamp),
            )

    def queue_position(self, identifier: str) -> int | None:
        return self.store.queue_position(identifier)

    def scheduler_snapshot(self) -> dict:
        queued = self.store.queued()
        guard_error = None
        try:
            decision = self.host_guard.evaluate()
        except RuntimeError as exc:
            decision = None
            guard_error = type(exc).__name__
        return {
            "host_id": self.config["runtime"]["host_id"],
            "concurrency_cap": self.store.concurrency_cap,
            "max_concurrency_cap": int(self.config["scheduler"]["max_concurrency_cap"]),
            "active": self.store.active_count(),
            "queued": len(queued),
            "guard_allowed": decision.allowed if decision else (False if queued else None),
            "guard_reason": decision.reason if decision else ("waiting for host capacity" if queued else f"host measurements unavailable ({guard_error})" if guard_error else None),
            "load_1m": decision.load_1m if decision else None,
            "load_limit": decision.load_limit if decision else None,
            "available_memory_bytes": decision.available_memory_bytes if decision else None,
        }

    def submit(self, url: str) -> dict:
        normalized = self.source.normalize(url)
        record = self.store.create(normalized)
        try:
            self.driver.ensure_ready()
        except Exception as exc:
            self.store.update(record["id"], status="failed", error=str(exc)[:1200])
            raise
        self._log(record["id"], "Queued repository; repository code will run only inside its isolated ARM64 container.")
        self._schedule()
        return self.store.get(record["id"])

    def submit_demo(self, name: str) -> dict:
        if self.config["demo"].get("fixtures_enabled") is not True:
            raise RepositoryError("bundled demo fixtures are disabled")
        if name not in self.source.fixture_names:
            raise RepositoryError("unknown acceptance fixture")
        record = self.store.create(f"demo://{name}", {"demo_fixture": name})
        try:
            self.driver.ensure_ready()
        except Exception as exc:
            self.store.update(record["id"], status="failed", error=str(exc)[:1200])
            raise
        self._log(record["id"], f"Queued bundled {name} acceptance fixture.")
        self._schedule()
        return self.store.get(record["id"])

    def _set_wait_reason(self, records: list[dict], reason: str) -> None:
        for record in records:
            if record.get("queue_reason") != reason:
                self.store.set_queue_reason(record["id"], reason)
                self._log(record["id"], f"Scheduler held job in queue: {reason}.")

    def _schedule(self) -> None:
        with self._scheduler_lock:
            while not self._stop.is_set():
                queued = self.store.queued()
                if not queued:
                    return
                try:
                    decision = self.host_guard.evaluate()
                except RuntimeError as exc:
                    self._set_wait_reason(queued, "waiting for host capacity")
                    self._log(queued[0]["id"], f"Host guard could not measure capacity; failing closed ({type(exc).__name__}).")
                    return
                if not decision.allowed:
                    self._set_wait_reason(queued, "waiting for host capacity")
                    self._log(queued[0]["id"], f"Host guard held queued work: load_1m={decision.load_1m:.2f}/{decision.load_limit:.2f}, mem_available={decision.available_memory_bytes}, required={decision.required_memory_bytes}.")
                    return
                if self.store.active_count() >= self.store.concurrency_cap:
                    self._set_wait_reason(queued, "waiting for scheduler capacity")
                    return
                claimed = self.store.claim_next()
                if not claimed:
                    return
                self._log(claimed["id"], f"Host guard allowed start: load_1m={decision.load_1m:.2f}/{decision.load_limit:.2f}, mem_available={decision.available_memory_bytes}, required={decision.required_memory_bytes}.")
                self._spawn(claimed)

    def _transition(self, identifier: str, status: str, error: str | None = None) -> dict:
        fields = {"status": status}
        if error is not None:
            fields["error"] = error[:1200]
        return self.store.update(identifier, **fields)

    def _run_job(self, identifier: str) -> None:
        record = self.store.get(identifier)
        if not record:
            return
        try:
            self._transition(identifier, "cloning")
            metadata = record.get("metadata") or {}
            if metadata.get("demo_fixture"):
                repo_path, sha, normalized = self.source.prepare_demo(metadata["demo_fixture"])
            else:
                repo_path, sha, normalized = self.source.prepare(record["url"])
            record = self.store.update(identifier, commit_sha=sha, repo_path=str(repo_path))
            self._log(identifier, f"Resolved commit {sha} from {normalized}.")

            self._transition(identifier, "detecting")
            plan = detect(repo_path)
            record = self.store.update(identifier, build_kind=plan.kind, app_port=plan.port, run_command=plan.run)
            self._log(identifier, f"Detected {plan.kind}; app port {plan.port}; ARM64 target enforced.")

            record = self.store.update(identifier, network_name=f"sm-net-{identifier}", egress_id=f"sm-edge-{identifier}")
            network, egress_id, host_port = self.driver.prepare_network(record, plan, lambda line: self._log(identifier, line))
            if network != record["network_name"] or egress_id != record["egress_id"]:
                raise DriverError("Docker returned unexpected sandbox network identifiers")
            record = self.store.update(identifier, host_port=host_port)
            self._transition(identifier, "building")
            self._log(identifier, "Building inside Docker with build egress constrained by the configured allowlist.")
            app_image = self.driver.build(record, repo_path, plan, network, lambda line: self._log(identifier, line))
            self.driver.release_build_network(network)
            if hasattr(self.driver, "build_wrapper"):
                image = self.driver.build_wrapper(record, app_image, lambda line: self._log(identifier, line))
            else:
                image = app_image
            record = self.store.update(identifier, image=image)

            self._transition(identifier, "starting")
            started = self.driver.start(record, image, plan, network, lambda line: self._log(identifier, line))
            try:
                record = self.store.update(
                    identifier,
                    container_id=started["container_id"],
                    container_name=started["container_name"],
                    host_port=started["host_port"],
                    image=image,
                    expires_at=time.time() + int(self.config["sandbox"]["default_ttl_seconds"]),
                    proxy_url=f"/s/{identifier}/",
                    agent_status="starting",
                    agent_started_at=time.time(),
                    agent_last_heartbeat=None,
                )
            except Exception:
                try:
                    self.driver.destroy({**record, **started, "network_name": network, "egress_id": egress_id})
                finally:
                    raise
            self._start_tailer(record)
            self._wait_healthy(started["host_port"])
            ticket = secrets.token_urlsafe(32)
            record = self.store.update(
                identifier,
                proxy_ticket_hash=hashlib.sha256(ticket.encode("ascii")).hexdigest(),
                proxy_url=f"/s/{identifier}/{ticket}/",
            )
            self._transition(identifier, "running", "")
            self._log(identifier, "Sandbox is ready through the authenticated control-plane proxy.")
        except (RepositoryError, DetectionError, ConfigError, DriverError, CapacityError, StateError) as exc:
            self._fail(identifier, str(exc))
        except Exception as exc:
            import traceback
            self._log(identifier, "Traceback (control-plane failure):\n" + traceback.format_exc())
            self._fail(identifier, f"unexpected control-plane failure ({type(exc).__name__})")
        finally:
            with self._lock:
                self._jobs.pop(identifier, None)
            self._schedule()

    def _wait_healthy(self, host_port: int) -> None:
        deadline = time.monotonic() + int(self.config["sandbox"]["healthcheck_timeout_seconds"])
        while time.monotonic() < deadline:
            try:
                request = urllib.request.Request(f"http://127.0.0.1:{host_port}/", headers={"Connection": "close"})
                with urllib.request.urlopen(request, timeout=1) as response:
                    response.read(512)
                return
            except urllib.error.HTTPError as exc:
                # A 502/503/504 comes from the ingress proxy when the app
                # is down or has not bound its port yet; it is not a
                # healthy app. Any other status — including the app's own
                # 500 — is the app itself answering (a dead app used to be
                # reported "running" on a 502, and a 500ing app used to
                # never pass at all).
                if exc.code not in {502, 503, 504}:
                    return
                time.sleep(0.2)
            except (OSError, urllib.error.URLError):
                time.sleep(0.2)
        raise DriverError("sandbox did not accept an HTTP connection before the health-check deadline")

    def _start_tailer(self, record: dict) -> None:
        identifier = record["id"]
        with self._lock:
            existing = self._tailers.get(identifier)
            if existing and existing[1].is_alive():
                return
            stopped = threading.Event()
            thread = threading.Thread(
                target=self.driver.follow_logs,
                args=(record, lambda line: self._observe_log_line(identifier, line), stopped),
                name=f"sandbox-logs-{identifier}",
                daemon=True,
            )
            self._tailers[identifier] = (stopped, thread)
            thread.start()

    def _stop_tailer(self, identifier: str) -> None:
        with self._lock:
            item = self._tailers.pop(identifier, None)
        if item:
            item[0].set()
            item[1].join(timeout=2)

    def _fail(self, identifier: str, message: str) -> None:
        message = message.replace("\x00", "")[:1200]
        self._log(identifier, f"Run failed: {message}")
        self._stop_tailer(identifier)
        record = self.store.get(identifier)
        if not record:
            return
        cleanup_ok = True
        try:
            self.driver.destroy(record)
        except Exception as cleanup_error:
            cleanup_ok = False
            message = f"{message}; sandbox cleanup failed ({type(cleanup_error).__name__})"[:1200]
            self._log(identifier, f"Cleanup failed ({type(cleanup_error).__name__}); resource identifiers were retained for retry.")
        fields = {"status": "failed", "error": message, "agent_status": "dead" if record.get("container_id") else "not-started"}
        if cleanup_ok:
            fields.update(
                container_id=None,
                container_name=None,
                host_port=None,
                network_name=None,
                egress_id=None,
                expires_at=None,
                proxy_url=None,
            proxy_ticket_hash=None,
            )
        try:
            self.store.update(identifier, **fields)
        except StateError:
            pass

    def get(self, identifier: str) -> dict | None:
        if not ID_PATTERN.fullmatch(identifier):
            return None
        return self.store.get(identifier)

    def list(self) -> list[dict]:
        return self.store.list()

    def stats(self, identifier: str) -> dict:
        record = self.get(identifier)
        if not record or record["status"] != "running":
            return {}
        try:
            return self.driver.stats(record)
        except DriverError:
            return {}

    def stop(self, identifier: str) -> dict:
        record = self.get(identifier)
        if not record:
            raise KeyError("sandbox not found")
        if record["status"] == "queued":
            result = self.store.cancel_queued(identifier, "stopped")
            self._log(identifier, "Queued sandbox stopped without contacting Docker.")
            self._schedule()
            return result
        if record["status"] != "running":
            raise StateError("only a running or queued sandbox can be stopped")
        self.store.update(identifier, status="stopping", agent_status="stopping")
        self._stop_tailer(identifier)
        try:
            self.driver.stop(record)
        except DriverError as exc:
            self.store.update(identifier, status="failed", error=f"sandbox cleanup failed: {exc}"[:1200])
            raise
        self._log(identifier, "Sandbox stopped; its container, egress proxy, and private network were removed.")
        result = self.store.update(
            identifier,
            status="stopped",
            agent_status="stopped",
            container_id=None,
            container_name=None,
            host_port=None,
            network_name=None,
            egress_id=None,
            expires_at=None,
            proxy_url=None,
            proxy_ticket_hash=None,
        )
        self._schedule()
        return result

    def restart(self, identifier: str) -> dict:
        record = self.get(identifier)
        if not record:
            raise KeyError("sandbox not found")
        if record["status"] not in {"stopped", "failed"}:
            raise StateError("only a stopped or failed sandbox can be restarted")
        if record.get("container_id") or record.get("network_name") or record.get("egress_id"):
            self.driver.destroy(record)
            self.store.update(
                identifier,
                container_id=None,
                container_name=None,
                host_port=None,
                network_name=None,
                egress_id=None,
                expires_at=None,
                proxy_url=None,
                proxy_ticket_hash=None,
            )
        record = self.store.requeue(identifier)
        self._log(identifier, "Restart requested; the cached commit and ARM64 image will be reused when available.")
        self._schedule()
        return self.store.get(identifier)

    def cancel(self, identifier: str) -> dict:
        record = self.get(identifier)
        if not record:
            raise KeyError("sandbox not found")
        if record["status"] != "queued":
            raise StateError("only a queued sandbox can be cancelled")
        result = self.store.cancel_queued(identifier, "destroyed")
        self._log(identifier, "Queued sandbox cancelled without contacting Docker.")
        self._schedule()
        return result

    def destroy(self, identifier: str) -> dict:
        record = self.get(identifier)
        if not record:
            raise KeyError("sandbox not found")
        if record["status"] == "queued":
            result = self.store.cancel_queued(identifier, "destroyed")
            self._log(identifier, "Queued sandbox destroyed without contacting Docker.")
            self._schedule()
            return result
        if record["status"] in {"cloning", "detecting", "building", "starting", "stopping"}:
            raise StateError("wait until the active operation finishes before destroying this sandbox")
        self._stop_tailer(identifier)
        if record["status"] == "running":
            self.store.update(identifier, status="stopping")
        last_error: DriverError | None = None
        for attempt in range(2):
            try:
                self.driver.destroy(record)
                last_error = None
                break
            except DriverError as exc:
                # The monitor's failed-job cleanup can be removing the same
                # resources at the same moment; a removal racing it fails
                # transiently. One retry after a short pause covers that
                # race; a still-failing destroy surfaces as before.
                last_error = exc
                if attempt == 0:
                    time.sleep(1)
        if last_error is not None:
            if record["status"] == "running":
                self.store.update(identifier, status="failed", error=f"sandbox cleanup failed: {last_error}"[:1200])
            raise last_error
        result = self.store.update(
            identifier,
            status="destroyed",
            container_id=None,
            container_name=None,
            host_port=None,
            network_name=None,
            egress_id=None,
            expires_at=None,
            proxy_url=None,
            proxy_ticket_hash=None,
        )
        self._log(identifier, "Sandbox destroyed; commit and image caches are retained for reuse.")
        self._schedule()
        return result

    def crash_app(self, identifier: str) -> dict:
        return self._kill_supervised_process(identifier, "app")

    def kill_agent(self, identifier: str) -> dict:
        return self._kill_supervised_process(identifier, "agent")

    def _kill_supervised_process(self, identifier: str, role: str) -> dict:
        record = self.get(identifier)
        if not record:
            raise KeyError("sandbox not found")
        if record.get("status") != "running":
            raise StateError("only a running sandbox process can be terminated")
        try:
            self.driver.kill_process(record, role)
        except DriverError:
            raise
        self._log(identifier, f"Supervisor test action terminated the {role} process; the sibling will be stopped by the supervisor.")
        return self.store.get(identifier)

    def resource_snapshot(self) -> dict:
        snapshot = self.driver.snapshot_all()
        return {f"managed_{name}": len(items) for name, items in snapshot.items() if isinstance(items, (list, tuple))}

    def logs(self, identifier: str, maximum: int = 200_000) -> str:
        record = self.get(identifier)
        if not record:
            raise KeyError("sandbox not found")
        path = self.logs_root / f"{identifier}.log"
        try:
            with path.open("rb") as stream:
                stream.seek(0, 2)
                size = stream.tell()
                stream.seek(max(0, size - min(maximum, 200_000)))
                text = stream.read().decode("utf-8", "replace")
        except FileNotFoundError:
            text = ""
        if record.get("container_id") and record.get("status") in {"running", "stopping"}:
            try:
                runtime = self.driver.logs(record, tail=200)
                if runtime and runtime not in text:
                    text = (text + "\n" + runtime).strip()
            except DriverError:
                pass
        return text[-min(maximum, 200_000):]

    def watch_logs(self, identifier: str, offset: int, stopped: threading.Event):
        path = self.logs_root / f"{identifier}.log"
        position = max(0, offset)
        while not stopped.is_set():
            try:
                size = path.stat().st_size
                if size < position:
                    position = 0
                if size > position:
                    with path.open("rb") as stream:
                        stream.seek(position)
                        block = stream.read(min(65536, size - position))
                    position += len(block)
                    if block:
                        yield block.decode("utf-8", "replace")
                        continue
            except FileNotFoundError:
                pass
            record = self.get(identifier)
            if not record or record["status"] in {"stopped", "failed", "destroyed"}:
                return
            if stopped.wait(0.25):
                return
            yield ""

    def proxy_target(self, identifier: str) -> tuple[str, int]:
        record = self.get(identifier)
        if not record or record["status"] != "running":
            raise KeyError("sandbox is not running")
        port = record.get("host_port")
        start, end = self.config["sandbox"]["port_range"]
        if not isinstance(port, int) or not start <= port <= end:
            raise DriverError("sandbox proxy target is outside the reserved loopback port range")
        return self.driver.proxy_target(record)

    def snapshot_all_docker_objects(self) -> dict:
        return self.driver.snapshot_all()

    def reap_expired(self) -> int:
        removed = 0
        for record in self.store.active():
            identifier = record["id"]
            if record["status"] == "failed" and any(record.get(key) for key in ("container_id", "container_name", "network_name", "egress_id")):
                try:
                    self.driver.destroy(record)
                    self.store.update(
                        identifier,
                        container_id=None,
                        container_name=None,
                        host_port=None,
                        network_name=None,
                        egress_id=None,
                        expires_at=None,
                        proxy_url=None,
            proxy_ticket_hash=None,
                    )
                    self._log(identifier, "Recovered failed-job Docker cleanup; reserved capacity is available again.")
                    removed += 1
                except Exception:
                    continue
                continue
            if record["status"] != "running":
                continue
            if record.get("agent_status") == "legacy":
                try:
                    alive = self.driver.container_exists(record)
                except DriverError:
                    continue
                if alive:
                    continue
                record = {**record, "agent_status": "dead"}
            else:
                try:
                    alive = self.driver.container_exists(record)
                except DriverError:
                    continue
            if alive:
                now = time.time()
                last = record.get("agent_last_heartbeat")
                started = record.get("agent_started_at")
                reference = float(last if last is not None else started if started is not None else record.get("updated_at", now))
                silent_after = float(self.config["agent"]["silent_after_seconds"])
                next_agent_status = "silent" if now - reference > silent_after else ("alive" if last is not None else "starting")
                if record.get("agent_status") != next_agent_status:
                    self.store.update(identifier, agent_status=next_agent_status)
                    if next_agent_status == "silent":
                        self._log(identifier, "Agent heartbeat is silent while the app container remains alive; reported without restarting the app.")
                continue
            self._stop_tailer(identifier)
            try:
                self.driver.destroy(record)
                self.store.update(
                    identifier,
                    status="failed",
                    agent_status="dead",
                    error="sandbox app or agent exited unexpectedly; its Docker resources were removed",
                    container_id=None,
                    container_name=None,
                    host_port=None,
                    network_name=None,
                    egress_id=None,
                    expires_at=None,
                    proxy_url=None,
            proxy_ticket_hash=None,
                )
                self._log(identifier, "Sandbox stopped unexpectedly; its egress proxy and private network were removed.")
                removed += 1
            except Exception as exc:
                try:
                    self.store.update(identifier, error=f"exit cleanup failed ({type(exc).__name__})")
                except StateError:
                    pass
        for record in self.store.active():
            if record["status"] != "failed" or not any(record.get(key) for key in ("container_id", "container_name", "network_name", "egress_id")):
                continue
            identifier = record["id"]
            try:
                self.driver.destroy(record)
                self.store.update(
                    identifier,
                    container_id=None,
                    container_name=None,
                    host_port=None,
                    network_name=None,
                    egress_id=None,
                    expires_at=None,
                    proxy_url=None,
            proxy_ticket_hash=None,
                    error="sandbox cleanup retry succeeded",
                )
                self._log(identifier, "Retried cleanup removed the remaining Docker resources.")
                removed += 1
            except Exception as exc:
                try:
                    self.store.update(identifier, error=f"sandbox cleanup retry failed ({type(exc).__name__})")
                except StateError:
                    pass
        for record in self.store.expired():
            identifier = record["id"]
            self._stop_tailer(identifier)
            try:
                self.driver.destroy(record)
                self.store.update(
                    identifier,
                    status="stopped",
                    container_id=None,
                    container_name=None,
                    host_port=None,
                    network_name=None,
                    egress_id=None,
                    expires_at=None,
                    proxy_url=None,
            proxy_ticket_hash=None,
                )
                self._log(identifier, "Sandbox stopped because its 30-minute TTL expired.")
                removed += 1
            except Exception as exc:
                try:
                    self.store.update(identifier, error=f"TTL cleanup failed ({type(exc).__name__})")
                except StateError:
                    pass
        return removed

    def startup_reconcile(self) -> None:
        self.driver.ensure_ready()
        active = self.store.active()
        running_ids = {record["id"] for record in active if record["status"] == "running"}
        self.driver.reconcile(running_ids)
        for record in active:
            identifier = record["id"]
            if record["status"] == "queued":
                continue
            if record["status"] == "running":
                if self.driver.container_exists(record):
                    self._start_tailer(record)
                    continue
                try:
                    self.driver.destroy(record)
                except DriverError:
                    pass
                self.store.update(
                    identifier,
                    status="failed",
                    agent_status="dead",
                    error="the sandbox app or agent container was missing after restart; its network resources were removed",
                    container_id=None,
                    container_name=None,
                    host_port=None,
                    network_name=None,
                    egress_id=None,
                    expires_at=None,
                    proxy_url=None,
            proxy_ticket_hash=None,
                )
            else:
                self.store.update(
                    identifier,
                    status="failed",
                    agent_status="not-started",
                    error="service restarted during an incomplete build; sandbox-owned Docker resources were removed",
                    container_id=None,
                    container_name=None,
                    host_port=None,
                    network_name=None,
                    egress_id=None,
                    expires_at=None,
                    proxy_url=None,
            proxy_ticket_hash=None,
                )
        self.driver.ensure_warm_pool()
        self._schedule()

    def start_background(self) -> None:
        if self._background and self._background.is_alive():
            return
        self._background = threading.Thread(target=self._reaper_loop, name="sandbox-ttl-reaper", daemon=True)
        self._background.start()

    def _reaper_loop(self) -> None:
        interval = max(0.2, float(self.config["scheduler"]["retry_interval_seconds"]))
        while not self._stop.wait(interval):
            try:
                self.reap_expired()
                self._schedule()
            except Exception:
                continue

    def stop_background(self) -> None:
        self._stop.set()
        with self._lock:
            tailers = list(self._tailers.values())
        for stopped, thread in tailers:
            stopped.set()
        for _, thread in tailers:
            thread.join(timeout=2)
        if self._background and self._background is not threading.current_thread():
            self._background.join(timeout=2)
        with self._lock:
            jobs = list(self._jobs.values())
        for thread in jobs:
            if thread is not threading.current_thread():
                thread.join(timeout=5)

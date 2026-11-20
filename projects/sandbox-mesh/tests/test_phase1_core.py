from __future__ import annotations

import base64
import email.message
import hashlib
import io
import json
import pathlib
import shutil
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from mesh.config import ConfigError, load_config, parse_yaml_subset, validate_config
from mesh.detector import DetectionError, detect
from mesh.detector import BuildPlan
from mesh.docker_driver import DockerDriver
from mesh.driver import DriverError
from mesh.egress_proxy import Handler as EgressHandler
from mesh.egress_proxy import read_client_hello, relay_chunked_body, relay_length_body, resolve_public
from mesh.git_source import GitSource
from mesh.ingress_proxy import Handler as IngressHandler
from mesh.orchestrator import Orchestrator
from mesh.repository import RepositoryError, canonical_source, _git_env
from mesh.server import APIApplication, AuthManager, RequestHandler
from mesh.state import CapacityError, StateError, StateStore


ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP_ROOT = ROOT / "tmp"
TMP_ROOT.mkdir(exist_ok=True)


def headers(**values):
    message = email.message.Message()
    for key, value in values.items():
        message[key.replace("_", "-")] = value
    return message


class ConfigAndDetectionTests(unittest.TestCase):
    def test_phase_two_config_derives_host_cap_and_preserves_isolation(self):
        config = load_config(ROOT)
        self.assertEqual(config["host_id"], "foundry")
        self.assertEqual(config["driver"], "docker")
        self.assertEqual(config["resources"]["concurrency_cap"], 1)
        self.assertTrue(config["features"]["phase_1"])
        self.assertTrue(config["features"]["phase_2"])
        self.assertEqual([config["features"][f"phase_{n}"] for n in range(3, 6)], [False] * 3)
        self.assertEqual(config["runtime"]["concurrency_cap"], min(config["scheduler"]["max_concurrency_cap"], max(1, config["runtime"]["cpu_count"])))
        self.assertEqual(config["scheduler"]["host_concurrency_caps"]["foundry"], 1)
        self.assertEqual(config["sandbox"]["port_range"], [8900, 8999])
        self.assertTrue(config["egress"]["default_deny"])

    def test_config_rejects_fallback_and_resource_widening(self):
        config = load_config(ROOT)
        config["driver"] = "userns"
        with self.assertRaises(ConfigError):
            validate_config(config)
        config = load_config(ROOT)
        config["sandbox"]["pids_limit"] = 129
        with self.assertRaises(ConfigError):
            validate_config(config)

    def test_repeated_yaml_keys_and_unsafe_yaml_features_are_rejected(self):
        with self.assertRaises(ConfigError):
            parse_yaml_subset("a: 1\na: 2\n")
        with self.assertRaises(ConfigError):
            parse_yaml_subset("a: !!python/object/apply:os.system [true]\n")

    def test_dockerfile_precedes_other_markers_and_overrides_are_applied(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
            repo = pathlib.Path(temporary)
            (repo / "Dockerfile").write_text("FROM python:3.12-slim\nEXPOSE 8090\n", encoding="utf-8")
            (repo / "package.json").write_text("{}", encoding="utf-8")
            (repo / "sandbox.yaml").write_text('run: ["python3", "app.py"]\nport: 9000\n', encoding="utf-8")
            plan = detect(repo)
            self.assertEqual(plan.kind, "dockerfile")
            self.assertEqual(plan.port, 9000)
            self.assertEqual(plan.run, ["python3", "app.py"])

    def test_dockerfile_remote_sources_network_and_arch_overrides_fail_closed(self):
        bad_files = (
            "FROM python:3.12-slim\nADD https://example.test/payload /tmp/payload\n",
            "FROM python:3.12-slim\nRUN --network=host echo no\n",
            "FROM --platform=linux/amd64 python:3.12-slim\n",
        )
        for dockerfile in bad_files:
            with self.subTest(dockerfile=dockerfile), tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
                repo = pathlib.Path(temporary)
                (repo / "Dockerfile").write_text(dockerfile, encoding="utf-8")
                with self.assertRaises(DetectionError):
                    detect(repo)

    def test_sandbox_yaml_cannot_change_isolation_or_egress(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
            repo = pathlib.Path(temporary)
            (repo / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
            (repo / "sandbox.yaml").write_text("network: host\n", encoding="utf-8")
            with self.assertRaises(DetectionError):
                detect(repo)

    def test_compose_host_network_and_privileged_services_are_rejected(self):
        fixtures = (
            "services:\n  web:\n    build: .\n    network_mode: host\n",
            "services:\n  web:\n    build: .\n    privileged: true\n",
        )
        for source in fixtures:
            with self.subTest(source=source), tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
                repo = pathlib.Path(temporary)
                compose = source.replace("build: .", "image: python:3.12-slim")
                (repo / "docker-compose.yml").write_text(compose, encoding="utf-8")
                with self.assertRaises(DetectionError):
                    detect(repo)


class RepositoryTests(unittest.TestCase):
    def test_only_canonical_github_https_urls_are_accepted(self):
        normalized, local = canonical_source("https://github.com/example/project", ROOT)
        self.assertEqual(normalized, "https://github.com/example/project.git")
        self.assertFalse(local)
        for url in (
            "http://github.com/example/project",
            "https://user:secret@github.com/example/project",
            "https://github.com/example/project/tree/main",
            "https://github.com/example/project?token=secret",
            "https://evil.test/example/project",
        ):
            with self.subTest(url=url), self.assertRaises(RepositoryError):
                canonical_source(url, ROOT)

    def test_private_token_is_configured_only_in_child_environment(self):
        token = "private-test-token-never-log"
        env = _git_env(token, ROOT)
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertIn("extraheader", env["GIT_CONFIG_KEY_0"])
        self.assertNotIn(token, env["GIT_CONFIG_VALUE_0"])
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")

    def test_demo_fixture_is_content_addressed_without_creating_a_git_commit(self):
        config = load_config(ROOT)
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
            project = pathlib.Path(temporary)
            shutil.copytree(ROOT / "samples", project / "samples")
            source = GitSource(project, config)
            path, sha, url = source.prepare_demo("node-basic")
            self.assertRegex(sha, r"^[0-9a-f]{64}$")
            self.assertEqual(url, "demo://node-basic")
            self.assertEqual(path, project / "cache" / "repos" / sha)
            self.assertFalse((path / ".git").exists())
            self.assertEqual(source.prepare_demo("node-basic")[1], sha)


def _tls_client_hello(host: str, extra_extensions: bytes = b"") -> bytes:
    server_name = host.encode("ascii")
    name = b"\x00" + len(server_name).to_bytes(2, "big") + server_name
    server_names = len(name).to_bytes(2, "big") + name
    sni_extension = b"\x00\x00" + len(server_names).to_bytes(2, "big") + server_names
    extensions = sni_extension + extra_extensions
    body = b"\x03\x03" + bytes(32) + b"\x00" + b"\x00\x02\x13\x01" + b"\x01\x00" + len(extensions).to_bytes(2, "big") + extensions
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake


class _FragmentedSocket:
    def __init__(self, payload: bytes):
        self.payload = bytearray(payload)
        self.sent = bytearray()
        self.timeouts = []

    def recv(self, size: int) -> bytes:
        size = min(size, 7)
        block = bytes(self.payload[:size])
        del self.payload[:size]
        return block

    def sendall(self, payload: bytes) -> None:
        self.sent.extend(payload)

    def settimeout(self, timeout) -> None:
        self.timeouts.append(timeout)


class EgressTests(unittest.TestCase):
    def test_sni_is_parsed_across_records_and_ech_is_denied(self):
        packet = _tls_client_hello("registry.example.test")
        handshake = packet[5:]
        split = len(handshake) // 2
        fragmented = b"\x16\x03\x01" + split.to_bytes(2, "big") + handshake[:split]
        fragmented += b"\x16\x03\x03" + (len(handshake) - split).to_bytes(2, "big") + handshake[split:]
        client = _FragmentedSocket(fragmented)
        name, forwarded = read_client_hello(client)
        self.assertEqual(name, "registry.example.test")
        self.assertEqual(forwarded, fragmented)
        ech = b"\xfe\x0d\x00\x01\x00"
        with self.assertRaisesRegex(ValueError, "Encrypted ClientHello"):
            read_client_hello(_FragmentedSocket(_tls_client_hello("registry.example.test", ech)))

    def test_connect_sni_mismatch_never_opens_an_upstream_socket(self):
        request = b"CONNECT registry.example.test:443 HTTP/1.1\r\nHost: registry.example.test:443\r\n\r\n"
        fake = _FragmentedSocket(request + _tls_client_hello("not-allowed.example"))
        handler = object.__new__(EgressHandler)
        handler.request = fake
        with mock.patch("mesh.egress_proxy.ALLOWLIST", {"registry.example.test"}), mock.patch("mesh.egress_proxy.connect_addresses") as connect:
            handler.handle()
        connect.assert_not_called()
        self.assertIn(b"200 Connection Established", fake.sent)
        self.assertEqual(len(fake.sent), len(b"HTTP/1.1 200 Connection Established\r\n\r\n"))


    def test_dns_is_exact_allowlist_and_private_addresses_are_blocked(self):
        with mock.patch("mesh.egress_proxy.ALLOWLIST", {"registry.example.test"}), mock.patch(
            "mesh.egress_proxy.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
        ):
            addresses = resolve_public("registry.example.test", 443)
            self.assertEqual(addresses[0][3][0], "8.8.8.8")
            with self.assertRaises(ValueError):
                resolve_public("other.example.test", 443)
        with mock.patch("mesh.egress_proxy.ALLOWLIST", {"registry.example.test"}), mock.patch(
            "mesh.egress_proxy.socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
        ):
            with self.assertRaises(ValueError):
                resolve_public("registry.example.test", 443)

    def test_content_length_and_chunked_uploads_are_framed_without_truncation(self):
        client, client_peer = socket.socketpair()
        upstream, upstream_peer = socket.socketpair()
        try:
            def send_remaining():
                client_peer.sendall(b"de")
                client_peer.close()
            sender = threading.Thread(target=send_remaining)
            sender.start()
            relay_length_body(client, upstream, b"abc", 5)
            self.assertEqual(upstream_peer.recv(5), b"abcde")
            sender.join(timeout=1)
        finally:
            client.close()
            client_peer.close()
            upstream.close()
            upstream_peer.close()

        client, client_peer = socket.socketpair()
        upstream, upstream_peer = socket.socketpair()
        try:
            relay_chunked_body(client, upstream, b"3\r\nabc\r\n0\r\n\r\n")
            self.assertEqual(upstream_peer.recv(64), b"3\r\nabc\r\n0\r\n\r\n")
        finally:
            client.close()
            client_peer.close()
            upstream.close()
            upstream_peer.close()


class StateTests(unittest.TestCase):
    def test_sqlite_queue_survives_reopen_and_claims_fifo_under_cap_one(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
            path = pathlib.Path(temporary) / "data" / "state.sqlite3"
            store = StateStore(path, 1)
            first = store.create("https://github.com/example/one.git", {"purpose": "test"})
            second = store.create("https://github.com/example/two.git")
            self.assertEqual(first["status"], "queued")
            self.assertEqual(store.queue_position(first["id"]), 1)
            self.assertEqual(store.queue_position(second["id"]), 2)
            claimed = store.claim_next()
            self.assertEqual(claimed["id"], first["id"])
            self.assertEqual(claimed["status"], "cloning")
            store.update(first["id"], status="detecting", run_command=["python3", "app.py"])
            reopened = StateStore(path, 1)
            recovered = reopened.get(first["id"])
            self.assertEqual(recovered["status"], "detecting")
            self.assertEqual(recovered["metadata"], {"purpose": "test"})
            self.assertEqual(recovered["run_command"], ["python3", "app.py"])
            self.assertEqual(reopened.queue_position(second["id"]), 1)
            self.assertIsNone(reopened.claim_next())
            with self.assertRaises(StateError):
                reopened.update(first["id"], status="running")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class FakeDriver:
    def __init__(self):
        self.ready_calls = 0
        self.events = []

    def ensure_ready(self):
        self.ready_calls += 1
        return {"engine": "fake"}

    def prepare_network(self, record, plan, on_output):
        self.events.append(("prepare_network", record["id"]))
        return f"sm-net-{record['id']}", f"sm-edge-{record['id']}", 8900

    def release_build_network(self, network):
        self.events.append(("release_build_network", network))

    def build(self, record, repo_path, plan, network_name, on_output):
        self.events.append(("build", plan.kind, network_name))
        return f"image:{record['commit_sha']}"

    def start(self, record, image, plan, network_name, on_output):
        self.events.append(("start", image, network_name))
        return {"container_id": "a" * 64, "container_name": f"sm-app-{record['id']}", "host_port": 8900, "image": image}

    def stop(self, record):
        self.events.append(("stop", record["id"]))

    def destroy(self, record):
        self.events.append(("destroy", record["id"]))

    def logs(self, record, tail=200):
        return "runtime marker"

    def follow_logs(self, record, emit, stopped):
        emit("runtime marker")
        stopped.wait(0.05)

    def stats(self, record):
        return {"cpu": "1%", "memory": "3MiB / 2GiB", "pids": "2"}

    def proxy_target(self, record):
        return "127.0.0.1", record["host_port"]

    def container_exists(self, record):
        return True

    def reconcile(self, active_ids):
        return []

    def ensure_warm_pool(self):
        return None

    def snapshot_all(self):
        return {"containers": [], "volumes": [], "networks": []}


class IngressProxyTests(unittest.TestCase):
    def test_upstream_host_is_loopback_and_user_credentials_are_not_forwarded(self):
        handler = object.__new__(IngressHandler)
        handler.command = "GET"
        handler.path = "/"
        handler.headers = email.message.Message()
        handler.headers["Cookie"] = "sandbox_mesh_session=secret"
        handler.headers["Authorization"] = "Bearer secret"
        handler.headers["X-Forwarded-Host"] = "attacker.example"
        handler.headers["X-Forwarded-Prefix"] = "/s/abcdef123456/ticket"
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.client_address = ("127.0.0.1", 12345)
        handler.close_connection = False
        response = mock.Mock()
        response.status = 200
        response.reason = "OK"
        response.getheaders.return_value = [("Content-Length", "0")]
        response.read1.return_value = b""
        connection = mock.Mock()
        connection.getresponse.return_value = response
        statuses = []
        headers = []
        handler.send_response = lambda status, reason=None: statuses.append(status)
        handler.send_header = lambda key, value: headers.append((key, value))
        handler.end_headers = lambda: None
        with mock.patch("mesh.ingress_proxy.UPSTREAM_HOST", "sm-app-abcdef123456"), mock.patch("mesh.ingress_proxy.UPSTREAM_PORT", 8080), mock.patch("mesh.ingress_proxy.http.client.HTTPConnection", return_value=connection):
            handler._proxy()
        forwarded = connection.request.call_args.kwargs["headers"]
        self.assertEqual(forwarded["Host"], "127.0.0.1:8080")
        self.assertNotIn("Cookie", forwarded)
        self.assertNotIn("Authorization", forwarded)
        self.assertNotIn("X-Forwarded-Host", forwarded)
        self.assertEqual(statuses, [200])


class OrchestratorTests(unittest.TestCase):
    def test_run_stop_restart_destroy_logs_proxy_and_capacity(self):
        config = load_config(ROOT)
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
            project = pathlib.Path(temporary)
            shutil.copytree(ROOT / "samples", project / "samples")
            store = StateStore(project / "data" / "state.sqlite3", 1)
            source = GitSource(project, config)
            driver = FakeDriver()
            orchestrator = Orchestrator(project, config, store, source, driver)
            orchestrator._wait_healthy = lambda host_port: None
            try:
                first = orchestrator.submit_demo("node-basic")
                item = self._wait(orchestrator, first["id"], "running")
                self.assertEqual(item["build_kind"], "buildpack")
                self.assertEqual(orchestrator.proxy_target(item["id"]), ("127.0.0.1", 8900))
                self.assertIn("runtime marker", orchestrator.logs(item["id"]))
                second = orchestrator.submit_demo("python-basic")
                self.assertEqual(second["status"], "queued")
                self.assertEqual(orchestrator.queue_position(second["id"]), 1)
                self.assertEqual(driver.ready_calls, 2)
                stopped = orchestrator.stop(item["id"])
                self.assertEqual(stopped["status"], "stopped")
                second_running = self._wait(orchestrator, second["id"], "running")
                restarted = orchestrator.restart(item["id"])
                self.assertEqual(restarted["status"], "queued")
                destroyed_second = orchestrator.destroy(second_running["id"])
                self.assertEqual(destroyed_second["status"], "destroyed")
                self.assertEqual(self._wait(orchestrator, item["id"], "running")["status"], "running")
                destroyed = orchestrator.destroy(item["id"])
                self.assertEqual(destroyed["status"], "destroyed")
                self.assertTrue(any(event[0] == "release_build_network" for event in driver.events))
            finally:
                orchestrator.stop_background()

    def test_wait_healthy_ignores_502_until_app_answers(self):
        import http.server
        config = load_config(ROOT)
        config["sandbox"]["healthcheck_timeout_seconds"] = 5
        calls = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(1)
                self.send_response(502 if len(calls) <= 2 else 200)
                self.send_header("Content-Length", "0")
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
                project = pathlib.Path(temporary)
                shutil.copytree(ROOT / "samples", project / "samples")
                store = StateStore(project / "data" / "state.sqlite3", 1)
                orchestrator = Orchestrator(project, config, store, GitSource(project, config), FakeDriver())
                try:
                    orchestrator._wait_healthy(server.server_address[1])
                finally:
                    orchestrator.stop_background()
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()
        self.assertEqual(len(calls), 3)

    def test_wait_healthy_fails_when_app_only_returns_502(self):
        import http.server
        config = load_config(ROOT)
        config["sandbox"]["healthcheck_timeout_seconds"] = 1
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
                project = pathlib.Path(temporary)
                shutil.copytree(ROOT / "samples", project / "samples")
                store = StateStore(project / "data" / "state.sqlite3", 1)
                orchestrator = Orchestrator(project, config, store, GitSource(project, config), FakeDriver())
                try:
                    with self.assertRaises(DriverError):
                        orchestrator._wait_healthy(server.server_address[1])
                finally:
                    orchestrator.stop_background()
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_wait_healthy_accepts_the_apps_own_500(self):
        import http.server
        config = load_config(ROOT)
        config["sandbox"]["healthcheck_timeout_seconds"] = 5
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
                project = pathlib.Path(temporary)
                shutil.copytree(ROOT / "samples", project / "samples")
                store = StateStore(project / "data" / "state.sqlite3", 1)
                orchestrator = Orchestrator(project, config, store, GitSource(project, config), FakeDriver())
                try:
                    orchestrator._wait_healthy(server.server_address[1])
                finally:
                    orchestrator.stop_background()
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    @staticmethod
    def _wait(orchestrator, identifier: str, status: str, timeout: float = 3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = orchestrator.get(identifier)
            if record and record["status"] == status:
                return record
            if record and record["status"] == "failed":
                raise AssertionError(record["error"])
            time.sleep(0.01)
        raise AssertionError(f"sandbox did not reach {status}")


class FakeAPIOrchestrator:
    def __init__(self):
        self.calls = []
        self.record = {
            "id": "a" * 12,
            "url": "https://github.com/example/repo.git",
            "status": "running",
            "created_at": 1.0,
            "updated_at": 2.0,
            "commit_sha": "b" * 40,
            "build_kind": "dockerfile",
            "app_port": 8080,
            "proxy_url": f"/s/{'a' * 12}/{'A' * 43}/",
            "proxy_ticket_hash": hashlib.sha256(b"A" * 43).hexdigest(),
            "host_port": 8900,
        }
        self.logs_root = ROOT / "logs"

    def submit(self, url):
        self.calls.append(("submit", url))
        self.record["status"] = "queued"
        return self.record.copy()

    def submit_demo(self, name):
        self.calls.append(("submit_demo", name))
        return self.record.copy()

    def list(self):
        return [self.record.copy()]

    def get(self, identifier):
        return self.record.copy() if identifier == self.record["id"] else None

    def logs(self, identifier):
        self.calls.append(("logs", identifier))
        return "test logs"

    def watch_logs(self, identifier, offset, stopped):
        self.calls.append(("watch_logs", identifier, offset))
        yield "live marker"

    def stats(self, identifier):
        return {"cpu": "1%", "memory": "2MiB", "pids": "1"}

    def stop(self, identifier):
        self.calls.append(("stop", identifier))
        self.record["status"] = "stopped"
        return self.record.copy()

    def restart(self, identifier):
        self.calls.append(("restart", identifier))
        self.record["status"] = "running"
        return self.record.copy()

    def destroy(self, identifier):
        self.calls.append(("destroy", identifier))
        self.record["status"] = "destroyed"
        return self.record.copy()

    def proxy_target(self, identifier):
        return "127.0.0.1", 8900


class FakeProxyHandler:
    def __init__(self):
        self.headers = headers(
            Host="127.0.0.1:8787",
            Cookie="sm_session=private",
            Authorization="Bearer should-not-forward",
            Accept="text/plain",
            X_Forwarded_Host="attacker.test",
            Origin="null",
        )
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.response_headers = []
        self.status = None
        self.close_connection = False

    def send_error(self, status, message=None):
        self.status = status

    def send_response(self, status, reason=None):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        return None


class APITests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT)
        self.token = "phase1-test-token-with-32-characters"
        self.auth = AuthManager(self.token, 300)
        self.orchestrator = FakeAPIOrchestrator()
        self.app = APIApplication(ROOT, self.config, self.orchestrator, self.auth)
        login = self.app.dispatch("POST", "/api/login", headers(Host="127.0.0.1:8787", Authorization="Bearer " + self.token), b"{}")
        self.assertEqual(login.status, 200)
        cookie_value = login.headers["Set-Cookie"].split(";", 1)[0]
        self.session = headers(Host="127.0.0.1:8787", Cookie=cookie_value, X_CSRF_Token=json.loads(login.body)["csrf"])

    def test_login_required_csrf_checked_and_all_public_actions_route(self):
        self.assertEqual(self.app.dispatch("GET", "/api/sandboxes", headers(Host="127.0.0.1:8787"), b"").status, 401)
        self.assertEqual(self.app.dispatch("POST", "/api/sandboxes", headers(Host="127.0.0.1:8787", Cookie=self.session["Cookie"]), b'{"url":"https://github.com/example/repo"}').status, 403)
        self.assertEqual(self.app.dispatch("POST", "/api/sandboxes", self.session, b'{"url":"https://github.com/example/repo"}').status, 202)
        self.assertEqual(self.orchestrator.calls[-1][0], "submit")
        identifier = "a" * 12
        self.assertEqual(self.app.dispatch("GET", f"/api/sandboxes/{identifier}/logs", self.session, b"").status, 200)
        for action in ("stop", "restart", "destroy"):
            response = self.app.dispatch("POST", f"/api/sandboxes/{identifier}/{action}", self.session, b"")
            self.assertEqual(response.status, 200)
            self.assertEqual(self.orchestrator.calls[-1][0], action)

    def test_proxy_drops_credentials_streams_body_and_rewrites_paths(self):
        handler = FakeProxyHandler()
        response = mock.Mock()
        response.status = 200
        response.reason = "OK"
        response.getheaders.return_value = [
            ("Content-Type", "text/plain"),
            ("Transfer-Encoding", "chunked"),
            ("Location", "/next"),
            ("Set-Cookie", "sid=x; Path=/"),
        ]
        response.getheader.return_value = "text/plain"
        response.read1.side_effect = [b"streamed-", b"body", b""]
        connection = mock.Mock()
        connection.getresponse.return_value = response
        ticket = "A" * 43
        with mock.patch("mesh.server.http.client.HTTPConnection", return_value=connection):
            self.app.proxy(handler, "GET", f"/s/{'a' * 12}/{ticket}/?x=1")
        sent_headers = connection.request.call_args.kwargs["headers"]
        self.assertNotIn("Cookie", sent_headers)
        self.assertNotIn("Authorization", sent_headers)
        self.assertNotIn("Origin", sent_headers)
        self.assertNotIn("X-Forwarded-Host", sent_headers)
        self.assertEqual(sent_headers["Host"], "127.0.0.1:8900")
        self.assertEqual(sent_headers["X-Forwarded-Prefix"], f"/s/{'a' * 12}/{ticket}")
        self.assertIn(("Location", f"/s/{'a' * 12}/{ticket}/next"), handler.response_headers)
        self.assertNotIn(("Set-Cookie", "sid=x; Path=/"), handler.response_headers)
        self.assertIn(("Access-Control-Allow-Origin", "null"), handler.response_headers)
        self.assertEqual(handler.wfile.getvalue(), b"streamed-body")

    def test_sandbox_html_is_path_scoped_and_runs_in_opaque_origin(self):
        from mesh.server import _sandbox_html
        ticket_path = f"/s/{'a' * 12}/{'A' * 43}"
        result = _sandbox_html(b'<html><head></head><a href="/settings">x</a><script>fetch("/api/data")</script></html>', ticket_path)
        text = result.decode()
        self.assertIn(f'<base href="{ticket_path}/">', text)
        self.assertIn(f'href="{ticket_path}/settings"', text)
        self.assertIn("window.fetch=function", text)
        self.assertIn('fetch("/api/data")', text)

    def test_html_proxy_sets_sandbox_csp_and_rewrites_the_response(self):
        handler = FakeProxyHandler()
        response = mock.Mock()
        response.status = 200
        response.reason = "OK"
        response.getheaders.return_value = [("Content-Type", "text/html; charset=utf-8")]
        response.getheader.side_effect = lambda name, default=None: "text/html; charset=utf-8" if name == "Content-Type" else default
        response.read.return_value = b'<html><head></head><a href="/settings">x</a></html>'
        connection = mock.Mock()
        connection.getresponse.return_value = response
        ticket = "A" * 43
        identifier = "a" * 12
        with mock.patch("mesh.server.http.client.HTTPConnection", return_value=connection):
            self.app.proxy(handler, "GET", f"/s/{identifier}/{ticket}/")
        headers_out = dict(handler.response_headers)
        self.assertIn("sandbox allow-scripts allow-forms allow-downloads", headers_out["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers_out["Content-Security-Policy"])
        self.assertIn(f'<base href="/s/{identifier}/{ticket}/">', handler.wfile.getvalue().decode())
        self.assertIn(f'href="/s/{identifier}/{ticket}/settings"', handler.wfile.getvalue().decode())

    def test_websocket_accepts_masked_frames_only(self):
        class FakeSocket:
            def __init__(self, value):
                self.input = io.BytesIO(value)
                self.output = bytearray()

            def recv(self, size):
                return self.input.read(size)

            def sendall(self, value):
                self.output.extend(value)

        mask = b"abcd"
        payload = b"hi"
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        frame = bytes([0x89, 0x80 | len(payload)]) + mask + masked
        handler = object.__new__(RequestHandler)
        handler.connection = FakeSocket(frame)
        self.assertTrue(handler._read_ws_frame())
        self.assertEqual(bytes(handler.connection.output), b"\x8a\x02hi")
        handler.connection = FakeSocket(b"\x89\x02hi")
        self.assertIsNone(handler._read_ws_frame())

    def test_authenticated_websocket_streams_runtime_logs(self):
        class FakeSocket:
            def __init__(self):
                self.output = bytearray()

            def settimeout(self, timeout):
                self.timeout = timeout

            def sendall(self, value):
                self.output.extend(value)

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
            self.orchestrator.logs_root = pathlib.Path(temporary) / "logs"
            handler = object.__new__(RequestHandler)
            handler.server = mock.Mock(app=self.app)
            handler.headers = headers(
                Host="127.0.0.1:8787",
                Origin="http://127.0.0.1:8787",
                Connection="Upgrade",
                Upgrade="websocket",
                Sec_WebSocket_Key="dGhlIHNhbXBsZSBub25jZQ==",
                Sec_WebSocket_Version="13",
                Cookie=self.session["Cookie"],
            )
            handler.path = f"/api/sandboxes/{'a' * 12}/logs/ws?offset=0"
            handler.request_version = "HTTP/1.1"
            handler.command = "GET"
            handler.requestline = "GET " + handler.path + " HTTP/1.1"
            handler.connection = FakeSocket()
            handler.wfile = io.BytesIO()
            handler.close_connection = False
            with mock.patch("mesh.server.select.select", return_value=([], [], [])):
                handler.do_ws()
        self.assertIn(b"101 Switching Protocols", handler.wfile.getvalue())
        self.assertIn(b"test logs", handler.connection.output)
        self.assertIn(b"live marker", handler.connection.output)
        self.assertIn(("watch_logs", "a" * 12, 0), self.orchestrator.calls)


class DockerRuntimePolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT)
        self.driver = DockerDriver(ROOT, self.config)
        self.record = {"id": "a" * 12, "commit_sha": "b" * 40, "host_port": 8900}

    def test_runtime_container_is_loopback_only_nonroot_and_resource_capped(self):
        plan = BuildPlan("dockerfile", ROOT, ROOT / "Dockerfile", None, 8080, None, None, [], {})
        with mock.patch.object(self.driver, "_capture", return_value="null"), mock.patch.object(self.driver, "_command", side_effect=["c" * 12, ""]) as command:
            result = self.driver.start(self.record, "sandbox-mesh/app:" + "b" * 40, plan, "sm-net-" + "a" * 12, lambda line: None)
        argv = command.call_args_list[0].args[0]
        container_argv = argv[argv.index("create"):]
        self.assertEqual(result["host_port"], 8900)
        self.assertIn("--user", argv)
        self.assertEqual(argv[argv.index("--user") + 1], self.config["sandbox"]["user"])
        self.assertIn("--read-only", argv)
        self.assertEqual(argv[argv.index("--sysctl") + 1], "net.ipv4.ip_unprivileged_port_start=0")


        self.assertIn("--cap-drop", argv)
        self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")
        self.assertEqual(argv[argv.index("--pids-limit") + 1], "128")
        self.assertEqual(argv[argv.index("--memory") + 1], "1610612736")
        self.assertEqual(argv[argv.index("--cpus") + 1], "0.7")
        self.assertIn("compress=false", argv)
        self.assertIn("max-file=1", argv)
        self.assertFalse(any(str(item).startswith("PATH=") for item in argv))
        self.assertNotIn("--publish", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "sm-net-" + "a" * 12)
        self.assertNotIn("--privileged", container_argv)
        self.assertNotIn("--mount", container_argv)
        self.assertNotIn("--volume", container_argv)

    def test_buildpack_start_merges_nixpacks_path_into_path(self):
        # Nixpacks puts provider binaries (a pip venv) in NIXPACKS_PATH and
        # merges it into PATH only via /root/.profile, which the non-root
        # app user never reads — so `gunicorn` was not found (exit 127).
        env = json.dumps(["PATH=/nix/var/nix/profiles/default/bin:/usr/bin:/bin", "NIXPACKS_PATH=/opt/venv/bin:"])
        def fake_capture(argv, **kwargs):
            if "Config.Volumes" in " ".join(argv):
                return "null"
            if "Config.Env" in " ".join(argv):
                return env
            raise AssertionError(f"unexpected capture: {argv}")
        plan = BuildPlan("buildpack", ROOT, None, None, 8000, ["gunicorn", "--config", "gunicorn.conf.py", "gettingstarted.wsgi"], None, [], {})
        with mock.patch.object(self.driver, "_capture", side_effect=fake_capture), mock.patch.object(self.driver, "_command", side_effect=["c" * 12, ""]) as command:
            self.driver.start(self.record, "sandbox-mesh/app:" + "b" * 40, plan, "sm-net-" + "a" * 12, lambda line: None)
        argv = command.call_args_list[0].args[0]
        self.assertIn("PATH=/opt/venv/bin:/nix/var/nix/profiles/default/bin:/usr/bin:/bin", argv)

    def test_cleanup_names_the_resource_that_failed(self):
        with mock.patch.object(self.driver, "_remove_container", side_effect=DriverError("boom")), mock.patch.object(self.driver, "_remove_network", return_value=None):
            with self.assertRaises(DriverError) as ctx:
                self.driver._cleanup({"id": "a" * 12})
        self.assertIn("app container", str(ctx.exception))
        self.assertIn("boom", str(ctx.exception))

    def test_build_steps_use_the_isolated_builder_and_cpu_memory_pid_caps(self):
        with mock.patch.object(self.driver, "_capture", return_value="172.25.0.2"):
            argv = self.driver._buildx_args(self.record, "sandbox-mesh/app:" + "b" * 40, ROOT, ROOT / "Dockerfile", "sm-net-" + "a" * 12, 900)
        self.assertIn("--network", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "host")
        self.assertIn("--allow", argv)
        self.assertIn("network.host", argv)
        self.assertIn("--add-host", argv)
        self.assertEqual(argv[argv.index("--add-host") + 1], "mesh-egress:172.25.0.2")
        self.assertIn("memory=1610612736", argv)
        self.assertIn("cpu-quota=70000", argv)
        self.assertIn("nproc=128:128", argv)
        self.assertIn("--platform", argv)
        self.assertEqual(argv[argv.index("--platform") + 1], "linux/arm64")
        self.assertNotIn("--privileged", argv)
        build_argv = argv[argv.index("build"):]
        self.assertFalse(any("docker.sock" in item for item in build_argv))

    def test_failure_snapshot_counts_only_sandbox_mesh_containers_networks_and_volumes(self):
        with mock.patch.object(self.driver, "_capture", side_effect=["warm\n", "job-network\n", ""] ) as capture:
            snapshot = self.driver.snapshot_all()
        self.assertEqual(snapshot, {"containers": ["warm"], "networks": ["job-network"], "volumes": []})
        self.assertEqual(capture.call_count, 3)
        for call in capture.call_args_list:
            argv = call.args[0]
            self.assertIn("--filter", argv)
            self.assertIn("label=com.sandbox-mesh.managed=true", argv)
        self.assertNotIn("images", snapshot)

    def test_proxy_readiness_wait_returns_once_ingress_listens(self):
        import http.server
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            self.driver._wait_proxy_ready("sm-edge-" + "a" * 12, server.server_address[1], timeout=5)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_proxy_readiness_wait_fails_closed_when_ingress_never_listens(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaises(DriverError):
            self.driver._wait_proxy_ready("sm-edge-" + "a" * 12, port, timeout=0.2)

    def test_egress_proxy_and_builder_only_join_the_per_job_network(self):
        commands = []
        def run(argv, **kwargs):
            commands.append(argv)
            return ""
        plan = BuildPlan("dockerfile", ROOT, ROOT / "Dockerfile", None, 8080, None, None, [], {})
        with mock.patch.object(self.driver, "allocate_host_port", return_value=8900), mock.patch.object(self.driver, "_isolate_buildkit_networks"), mock.patch.object(self.driver, "_wait_proxy_ready"), mock.patch.object(self.driver, "_command", side_effect=run), mock.patch.object(self.driver, "_capture", return_value="true"):
            network, proxy, port = self.driver.prepare_network(self.record, plan, lambda line: None)
        self.assertEqual(network, "sm-net-" + "a" * 12)
        self.assertEqual(proxy, "sm-edge-" + "a" * 12)
        self.assertEqual(port, 8900)
        internal, outbound = commands[0], commands[1]
        proxy_run = next(command for command in commands if "run" in command)
        proxy_container_argv = proxy_run[proxy_run.index("run"):]
        self.assertIn("--internal", internal)
        self.assertIn("com.docker.network.bridge.gateway_mode_ipv4=isolated", internal)
        self.assertNotIn("--internal", outbound)
        self.assertEqual(proxy_run[proxy_run.index("--network") + 1], "sm-out-" + "a" * 12)
        self.assertEqual(proxy_run[proxy_run.index("--publish") + 1], "127.0.0.1:8900:8081/tcp")
        self.assertNotIn("--privileged", proxy_container_argv)
        self.assertNotIn("--mount", proxy_container_argv)
        self.assertNotIn("--volume", proxy_container_argv)
        self.assertTrue(any("connect" in command and network in command for command in commands))

    def test_nixpacks_build_context_contains_repo_and_generated_files(self):
        repo = pathlib.Path(tempfile.mkdtemp(prefix="nixpacks-repo-", dir=TMP_ROOT))
        try:
            (repo / "package.json").write_text("{}\n", encoding="utf-8")
            (repo / "package-lock.json").write_text("{}\n", encoding="utf-8")
            (repo / "index.js").write_text("console.log(1)\n", encoding="utf-8")
            (repo / ".git").mkdir()
            (repo / ".git" / "config").write_text("[core]\n", encoding="utf-8")
            plan = BuildPlan("buildpack", repo, None, None, 3000, ["node", "index.js"], None, [], {})
            captured = {}
            def fake_command(argv, **kwargs):
                out = pathlib.Path(argv[argv.index("--out") + 1])
                generated = out / ".nixpacks"
                generated.mkdir(parents=True)
                (generated / "Dockerfile").write_text("FROM scratch\nCOPY .nixpacks/test.nix .nixpacks/test.nix\nCOPY . /app/.\n", encoding="utf-8")
                (generated / "test.nix").write_text("{}\n", encoding="utf-8")
                return ""
            def fake_buildx(record, image, context, dockerfile, network, on_output):
                # The staging dir is removed by _build_nixpacks' finally block,
                # so snapshot its contents while it still exists.
                captured["context"] = context
                captured["dockerfile"] = dockerfile
                captured["has_lockfile"] = (context / "package-lock.json").is_file()
                captured["has_index"] = (context / "index.js").is_file()
                captured["has_nix"] = (context / ".nixpacks" / "test.nix").is_file()
                captured["has_git"] = (context / ".git").exists()
                captured["is_repo"] = context.resolve() == repo.resolve()
            with mock.patch.object(self.driver, "_command", side_effect=fake_command), mock.patch.object(self.driver, "_run_buildx", side_effect=fake_buildx):
                self.driver._build_nixpacks(self.record, repo, plan, "sandbox-mesh/app:" + "b" * 40, "sm-net-" + "a" * 12, lambda line: None)
            self.assertTrue(captured["has_lockfile"])
            self.assertTrue(captured["has_index"])
            self.assertTrue(captured["has_nix"])
            self.assertFalse(captured["has_git"])
            self.assertFalse(captured["is_repo"])
            self.assertFalse((repo / ".nixpacks").exists())
        finally:
            shutil.rmtree(repo, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

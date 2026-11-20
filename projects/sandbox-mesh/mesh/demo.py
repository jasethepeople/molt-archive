from __future__ import annotations

import json
import math
import os
import pathlib
import statistics
import sys
import time
import urllib.error
import urllib.request
from http.cookies import SimpleCookie

from .config import ConfigError, load_config
from .docker_driver import DockerDriver


ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8787"
ACCEPTANCE_REPOS = (
    ("Node", "https://github.com/heroku/node-js-getting-started", "buildpack"),
    ("Python", "https://github.com/heroku/python-getting-started", "buildpack"),
    ("Dockerfile", "https://github.com/traefik/whoami", "dockerfile"),
)



class DemoError(RuntimeError):
    pass


class Client:
    def __init__(self, token: str):
        self.token = token
        self.cookie = ""
        self.csrf = ""

    def login(self) -> None:
        request = urllib.request.Request(
            BASE + "/api/login",
            data=b"{}",
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            raw_cookie = response.headers.get("Set-Cookie", "")
        cookie = SimpleCookie()
        cookie.load(raw_cookie)
        if "sm_session" not in cookie or not data.get("authenticated"):
            raise DemoError("control-plane login did not create an authenticated session")
        self.cookie = "sm_session=" + cookie["sm_session"].value
        self.csrf = data["csrf"]

    def call(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else b""
        headers = {"Cookie": self.cookie, "X-CSRF-Token": self.csrf}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(BASE + path, data=body if method in {"POST", "PUT", "PATCH", "DELETE"} else None, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(10_000_000)
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", "replace")
            raise DemoError(f"control API returned HTTP {exc.code}: {detail}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DemoError("control API returned invalid JSON") from exc

    def proxy(self, identifier: str) -> bytes:
        item = self.call("GET", f"/api/sandboxes/{identifier}").get("item", {})
        proxy_url = item.get("proxy_url")
        if not isinstance(proxy_url, str) or not proxy_url.startswith(f"/s/{identifier}/"):
            raise DemoError("sandbox capability URL is missing")
        request = urllib.request.Request(BASE + proxy_url)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.read(1_000_000)
        except urllib.error.HTTPError as exc:
            raise DemoError(f"reverse proxy returned HTTP {exc.code}") from exc


def wait_for_status(client: Client, identifier: str, expected: set[str], timeout: int = 1800) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.call("GET", f"/api/sandboxes/{identifier}")
        item = data["item"]
        if item["status"] in expected:
            return item
        if item["status"] in {"failed", "destroyed", "stopped"}:
            return item
        time.sleep(0.25)
    raise DemoError("sandbox did not reach a terminal or running state before the demo timeout")


def create_fixture(client: Client, name: str) -> tuple[str, float, dict]:
    started = time.monotonic()
    data = client.call("POST", f"/api/demo/{name}")
    identifier = data["item"]["id"]
    item = wait_for_status(client, identifier, {"running", "failed"})
    return identifier, time.monotonic() - started, item


def create_repository(client: Client, url: str) -> tuple[str, float, dict]:
    started = time.monotonic()
    data = client.call("POST", "/api/sandboxes", {"url": url})
    item = data.get("item")
    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
        raise DemoError("run API did not return a sandbox identifier")
    identifier = item["id"]
    item = wait_for_status(client, identifier, {"running", "failed"})
    if item["status"] != "running":
        raise DemoError(item.get("error") or f"sandbox ended in state {item['status']}")
    return identifier, time.monotonic() - started, item



def wait_for_log(client: Client, identifier: str, text: str, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        logs = client.call("GET", f"/api/sandboxes/{identifier}/logs").get("logs", "")
        if text in logs:
            return True
        time.sleep(0.25)
    return False


def record(failures: list[str], label: str, function) -> object | None:
    try:
        result = function()
        print(f"PASS  {label}")
        return result
    except Exception as exc:
        message = str(exc).replace("\n", " ")[:240]
        print(f"FAIL  {label}: {type(exc).__name__}: {message}")
        failures.append(label)
        return None


def exercise_repository(client: Client, failures: list[str], label: str, url: str, expected_kind: str, restart: bool = False) -> float | None:
    result = record(failures, f"{label}: GitHub clone, detect, ARM64 build, and start", lambda: create_repository(client, url))
    if result is None:
        return None
    identifier, elapsed, item = result
    try:
        if item["status"] != "running":
            raise DemoError(item.get("error") or f"sandbox ended in state {item['status']}")
        if item.get("build_type") != expected_kind:
            raise DemoError(f"detected {item.get('build_type')!r}, expected {expected_kind!r}")
        record(failures, f"{label}: reverse-proxy HTTP response", lambda: _assert_repo_proxy(client, identifier))
        record(failures, f"{label}: authenticated live logs", lambda: _assert_ready_log(client, identifier))
        if restart:
            stopped = record(failures, f"{label}: stop operation", lambda: client.call("POST", f"/api/sandboxes/{identifier}/stop"))
            if stopped is not None and stopped.get("item", {}).get("status") != "stopped":
                failures.append(f"{label}: stop result state")
                print(f"FAIL  {label}: stop result state")
            restarted = record(failures, f"{label}: restart operation", lambda: _restart(client, identifier))
            if restarted:
                record(failures, f"{label}: proxy after restart", lambda: _assert_repo_proxy(client, identifier))
        destroyed = record(failures, f"{label}: destroy operation", lambda: client.call("POST", f"/api/sandboxes/{identifier}/destroy"))
        if destroyed is not None and destroyed.get("item", {}).get("status") != "destroyed":
            failures.append(f"{label}: destroy result state")
            print(f"FAIL  {label}: destroy result state")
        return elapsed
    except DemoError as exc:
        # A failed status or a build-type mismatch is one repo's failure,
        # not a reason to abort the whole demo: record it and let main()
        # continue with the next repository.
        failures.append(f"{label}: {exc}")
        print(f"FAIL  {label}: {exc}")
        return None
    finally:
        try:
            current = client.call("GET", f"/api/sandboxes/{identifier}").get("item", {})
            if current.get("status") not in {"destroyed", "failed"}:
                client.call("POST", f"/api/sandboxes/{identifier}/destroy")
        except Exception:
            pass


def _assert_repo_proxy(client: Client, identifier: str) -> None:
    if not client.proxy(identifier).strip():
        raise DemoError("reverse proxy returned an empty application response")


def _assert_ready_log(client: Client, identifier: str) -> None:
    if not wait_for_log(client, identifier, "Sandbox is ready through the authenticated control-plane proxy."):
        raise DemoError("authenticated sandbox logs did not contain the ready event")


def _restart(client: Client, identifier: str) -> bool:
    result = client.call("POST", f"/api/sandboxes/{identifier}/restart")
    item = wait_for_status(client, identifier, {"running", "failed"})
    if item["status"] != "running":
        raise DemoError(item.get("error") or "sandbox did not restart")
    return True


def failure_cleanup(client: Client, failures: list[str]) -> None:
    try:
        driver = DockerDriver(ROOT, load_config(ROOT))
    except Exception as exc:
        failures.append("failed-build Docker driver setup")
        print(f"FAIL  failed-build Docker driver setup: {type(exc).__name__}: {str(exc)[:200]}")
        return
    before = record(failures, "failed-build baseline Docker snapshot", driver.snapshot_all)
    if before is None:
        return
    result = record(failures, "failure fixture: build fails without creating a sandbox", lambda: create_fixture(client, "build-failure"))
    if result is None:
        return
    identifier, _, item = result
    if item.get("status") != "failed":
        failures.append("failure fixture: expected failed state")
        print(f"FAIL  failure fixture: expected failed state, got {item.get('status')}")
    else:
        print("PASS  failure fixture: Docker reported the expected build failure")
    after = record(failures, "failed-build final Docker snapshot", lambda: _wait_snapshot(driver, before))
    if after is not None:
        if before == after:
            print("PASS  failed-build reliability: containers, volumes, and networks are unchanged")
        else:
            failures.append("failed-build reliability")
            before_counts = {key: len(value) for key, value in before.items()}
            after_counts = {key: len(value) for key, value in after.items()}
            print(f"FAIL  failed-build reliability: Docker object counts changed ({before_counts} -> {after_counts})")
    record(failures, "failure fixture: destroy record", lambda: client.call("POST", f"/api/sandboxes/{identifier}/destroy"))


def _wait_snapshot(driver, before: dict, timeout: float = 10.0) -> dict:
    """Snapshot Docker objects until a failed job's async cleanup settles.

    The monitor removes a failed job's edge container and network on its
    own pass, so a snapshot taken the instant the job reports failed can
    still show them. Poll until the snapshot matches the baseline (or the
    timeout expires, in which case the mismatch is reported as a failure).
    """
    deadline = time.monotonic() + timeout
    current = driver.snapshot_all()
    while current != before and time.monotonic() < deadline:
        time.sleep(0.5)
        current = driver.snapshot_all()
    return current


def benchmark(client: Client, failures: list[str], runs: int = 5, url: str = ACCEPTANCE_REPOS[0][1]) -> tuple[float, float] | None:
    warmup = record(failures, "benchmark warm-up: cached Node GitHub repository", lambda: create_repository(client, url))
    if warmup is None:
        return None
    identifier, _, item = warmup
    try:
        if item["status"] != "running":
            raise DemoError(item.get("error") or "benchmark warm-up failed")
        _assert_repo_proxy(client, identifier)
    except Exception as exc:
        failures.append("benchmark warm-up")
        print(f"FAIL  benchmark warm-up: {type(exc).__name__}: {str(exc)[:200]}")
        return None
    finally:
        try:
            client.call("POST", f"/api/sandboxes/{identifier}/destroy")
        except Exception as exc:
            failures.append("benchmark warm-up cleanup")
            print(f"FAIL  benchmark warm-up cleanup: {type(exc).__name__}")
    samples = []
    for index in range(runs):
        result = record(failures, f"cached GitHub run timing sample {index + 1}/{runs}", lambda: create_repository(client, url))
        if result is None:
            continue
        identifier, elapsed, item = result
        try:
            if item["status"] != "running":
                raise DemoError(item.get("error") or "cached sandbox failed to start")
            _assert_repo_proxy(client, identifier)
            samples.append(elapsed)
        except Exception as exc:
            failures.append(f"cached GitHub run timing sample {index + 1}")
            print(f"FAIL  cached GitHub run timing sample {index + 1}: {type(exc).__name__}: {str(exc)[:200]}")
        finally:
            try:
                client.call("POST", f"/api/sandboxes/{identifier}/destroy")
            except Exception as exc:
                failures.append(f"cached GitHub run cleanup {index + 1}")
                print(f"FAIL  cached GitHub run cleanup {index + 1}: {type(exc).__name__}")
    if not samples:
        return None
    p50 = statistics.median(samples)
    p95 = sorted(samples)[max(0, math.ceil(0.95 * len(samples)) - 1)]
    print(f"BENCHMARK  cached GitHub commit to running: n={len(samples)} p50={p50:.3f}s p95={p95:.3f}s target p50<3s p95<5s")
    if p50 >= 3 or p95 >= 5:
        failures.append("cached GitHub speed target")
        print("FAIL  cached GitHub speed target")
    else:
        print("PASS  cached GitHub speed target")
    update_report(p50, p95, len(samples))
    return p50, p95


def update_phase2_report(result: str) -> None:
    path = ROOT / "REPORT.md"
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    old = "| Live Phase 2 acceptance | Not run |"
    if old in text:
        text = text.replace(old, f"| Live Phase 2 acceptance | {result} |", 1)
        path.write_text(text, encoding="utf-8")


def update_report(p50: float, p95: float, count: int) -> None:
    path = ROOT / "REPORT.md"
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    old = "| Foundry cached GitHub commit to running | Not measured here | p50 < 3 s; p95 < 5 s |"
    new = f"| Foundry cached GitHub commit to running | n={count}; p50 {p50:.3f} s; p95 {p95:.3f} s | p50 < 3 s; p95 < 5 s |"
    if old in text:
        path.write_text(text.replace(old, new, 1), encoding="utf-8")


def main() -> int:
    global BASE
    BASE = os.environ.get("SANDBOX_MESH_URL", BASE).rstrip("/")
    if not BASE.startswith(("http://", "https://")):
        print("FAIL  demo setup: SANDBOX_MESH_URL must be an HTTP or HTTPS URL", file=sys.stderr)
        return 2
    token = os.environ.get("SANDBOX_MESH_TOKEN", "")
    if len(token) < 32:
        print("FAIL  demo setup: SANDBOX_MESH_TOKEN is not available in this shell", file=sys.stderr)
        return 2
    try:
        client = Client(token)
        client.login()
    except Exception as exc:
        print(f"FAIL  demo setup: {type(exc).__name__}: {str(exc)[:240]}", file=sys.stderr)
        return 2
    failures: list[str] = []
    from .phase2_demo import run_phase2
    phase2 = record(failures, "Phase 2: three-repository queue, supervisor, and heartbeat acceptance", lambda: run_phase2(client))
    failure_cleanup(client, failures)
    benchmark(client, failures)
    if failures:
        print(f"DEMO FAIL: {len(failures)} check(s) failed: " + ", ".join(failures))
        return 1
    if isinstance(phase2, dict):
        update_phase2_report(f"passed: {len(ACCEPTANCE_REPOS)} ARM64 repositories, peak concurrency {phase2['peak_active']}/{phase2['cap']}, FIFO queue, agent heartbeats, proxy, and lifecycle checks")
    print("DEMO PASS: three repositories submitted together, FIFO queue, ARM64 builds, live agent heartbeats, proxy, logs, stop/restart/destroy, failed-build cleanup, and cached GitHub timing passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

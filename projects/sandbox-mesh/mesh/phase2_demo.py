from __future__ import annotations

import os
import sys
import time

from . import demo as demo_module
from .demo import ACCEPTANCE_REPOS, Client, DemoError, wait_for_status


def _item(client: Client, identifier: str) -> dict:
    return client.call("GET", f"/api/sandboxes/{identifier}").get("item", {})


def _queue_ids(items: list[dict]) -> list[str]:
    positioned = [item for item in items if isinstance(item.get("queue_position"), int)]
    return [item["id"] for item in sorted(positioned, key=lambda item: item["queue_position"])]


def wait_for_agent(client: Client, identifier: str, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = _item(client, identifier)
        if item.get("status") == "running":
            heartbeat = item.get("agent_last_heartbeat")
            if item.get("agent_status") == "alive" and isinstance(heartbeat, (int, float)) and time.time() - heartbeat <= 15:
                return item
        elif item.get("status") in {"failed", "destroyed", "stopped"}:
            raise DemoError(item.get("error") or f"sandbox {identifier} ended in {item.get('status')} before its agent heartbeated")
        time.sleep(0.5)
    raise DemoError(f"agent in sandbox {identifier} did not emit a recent heartbeat")


def _wait_for_running(client: Client, identifier: str, timeout: float = 1800) -> dict:
    item = wait_for_status(client, identifier, {"running", "failed"}, timeout=timeout)
    if item.get("status") != "running":
        raise DemoError(item.get("error") or f"sandbox {identifier} ended in {item.get('status')}")
    return item


def _check_running(client: Client, identifier: str, label: str, expected_kind: str) -> None:
    item = _wait_for_running(client, identifier)
    if item.get("build_type") != expected_kind:
        raise DemoError(f"{label}: detected {item.get('build_type')!r}, expected {expected_kind!r}")
    wait_for_agent(client, identifier)
    if not client.proxy(identifier).strip():
        raise DemoError(f"{label}: application proxy returned an empty response")
    logs = client.call("GET", f"/api/sandboxes/{identifier}/logs").get("logs", "")
    if "agent-heartbeat " + identifier not in logs:
        raise DemoError(f"{label}: authenticated logs contain no agent heartbeat")


def _wait_for_failed(client: Client, identifier: str, timeout: float = 40) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = _item(client, identifier)
        if item.get("status") == "failed":
            return item
        if item.get("status") in {"destroyed", "stopped"}:
            raise DemoError(f"crashed sandbox {identifier} unexpectedly became {item.get('status')}")
        time.sleep(0.5)
    raise DemoError(f"sandbox {identifier} did not fail and clean up after its app process was killed")


def _resource_counts(client: Client) -> dict:
    resources = client.call("GET", "/api/resources")
    required = {"managed_containers", "managed_networks", "managed_volumes"}
    if not required.issubset(resources) or any(not isinstance(resources[key], int) for key in required):
        raise DemoError("managed Docker resource count response is incomplete")
    return resources


def _verify_app_crash(client: Client, submitted: list[tuple[str, str, str]], crash_id: str) -> None:
    before = _resource_counts(client)
    queued_before = client.call("GET", "/api/queue").get("items", [])
    if before["managed_containers"] < 1 or before["managed_networks"] < 1:
        raise DemoError("managed resource snapshot did not include the running crash-test sandbox")
    client.call("POST", f"/api/sandboxes/{crash_id}/crash-app")
    failed = _wait_for_failed(client, crash_id)
    if failed.get("agent_status") != "dead":
        raise DemoError("supervisor did not mark the agent dead when its app process exited")
    after = _resource_counts(client)
    if after["managed_containers"] != before["managed_containers"] - 1:
        raise DemoError("app crash did not remove exactly its managed container")
    if after["managed_networks"] != before["managed_networks"] - 1:
        raise DemoError("app crash did not remove exactly its managed network")
    for label, identifier, expected_kind in submitted:
        if identifier == crash_id:
            continue
        item = _item(client, identifier)
        if item.get("status") == "failed":
            raise DemoError(f"app crash incorrectly failed another job ({label})")
        if item.get("status") == "running":
            _check_running(client, identifier, label, expected_kind)
    failures = [identifier for _, identifier, _ in submitted if _item(client, identifier).get("status") == "failed"]
    if failures != [crash_id]:
        raise DemoError(f"expected only the app-crash sandbox to fail, got {failures!r}")
    if queued_before:
        next_queued = min(queued_before, key=lambda item: item.get("queue_position") or 0)["id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            next_item = _item(client, next_queued)
            if next_item.get("status") in {"cloning", "detecting", "building", "starting", "running"}:
                break
            if next_item.get("status") in {"failed", "destroyed", "stopped"}:
                raise DemoError(f"the next FIFO job {next_queued} was lost after app-crash cleanup")
            time.sleep(0.25)
        else:
            raise DemoError("queued work did not advance after app-crash cleanup")
    print("PASS  Phase 2: app crash failed one sandbox, stopped its agent, and removed only its managed container/network")


def run_phase2(client: Client) -> dict:
    scheduler = client.call("GET", "/api/scheduler")
    cap = scheduler.get("concurrency_cap")
    maximum_cap = scheduler.get("max_concurrency_cap")
    if not isinstance(cap, int) or not isinstance(maximum_cap, int) or not 1 <= cap <= maximum_cap:
        raise DemoError(f"invalid host concurrency cap: {cap!r}")
    existing = client.call("GET", "/api/sandboxes").get("items", [])
    busy = [item.get("id") for item in existing if item.get("status") in {"queued", "cloning", "detecting", "building", "starting", "running", "stopping"}]
    if busy:
        raise DemoError("run the three-repository acceptance only when no sandboxes are queued or active; existing jobs were left untouched")
    submitted: list[tuple[str, str, str]] = []
    failures: list[str] = []
    try:
        for label, url, expected_kind in ACCEPTANCE_REPOS:
            response = client.call("POST", "/api/sandboxes", {"url": url})
            item = response.get("item", {})
            identifier = item.get("id")
            if not isinstance(identifier, str) or len(identifier) != 12:
                raise DemoError(f"{label}: API did not return a sandbox identifier")
            submitted.append((label, identifier, expected_kind))
    except Exception:
        for _, identifier, _ in submitted:
            try:
                client.call("POST", f"/api/sandboxes/{identifier}/destroy")
            except Exception:
                pass
        raise
    identifiers = [identifier for _, identifier, _ in submitted]
    try:
        queue = client.call("GET", "/api/queue").get("items", [])
        queued_ids = [identifier for identifier in _queue_ids(queue) if identifier in identifiers]
        if queued_ids:
            currently_queued = [identifier for identifier in identifiers if _item(client, identifier).get("status") == "queued"]
            if queued_ids != currently_queued:
                raise DemoError(f"FIFO queue order differs from submission order: {queued_ids!r}")
        verified: set[str] = set()
        crash_id: str | None = None
        released: set[str] = set()
        deadline = time.monotonic() + 1800
        peak_active = 0
        expected_peak = min(cap, len(identifiers))
        while len(verified) < len(submitted):
            records = {identifier: _item(client, identifier) for identifier in identifiers}
            for label, identifier, expected_kind in submitted:
                status = records[identifier].get("status")
                if status == "failed":
                    if identifier == crash_id:
                        continue
                    raise DemoError(records[identifier].get("error") or f"{label} sandbox failed")
                if status == "running" and identifier not in verified:
                    _check_running(client, identifier, label, expected_kind)
                    verified.add(identifier)
                    print(f"PASS  Phase 2: {label} sandbox and agent are running")
            running = [identifier for identifier in identifiers if records[identifier].get("status") == "running"]
            active_count = sum(records[identifier].get("status") in {"cloning", "detecting", "building", "starting", "running", "stopping"} for identifier in identifiers)
            peak_active = max(peak_active, active_count)
            if crash_id is None and len(running) >= expected_peak:
                crash_id = next((identifier for identifier in running if identifier in verified), None)
                if crash_id is not None:
                    _verify_app_crash(client, submitted, crash_id)
                    print("PASS  Phase 2: app crash failed only its sandbox, released managed resources, and preserved queued or running peers")
                records = {identifier: _item(client, identifier) for identifier in identifiers}
            running = [identifier for identifier in identifiers if records[identifier].get("status") == "running"]
            queued = [identifier for identifier in identifiers if records[identifier].get("status") == "queued"]
            active_count = sum(records[identifier].get("status") in {"cloning", "detecting", "building", "starting", "running", "stopping"} for identifier in identifiers)
            peak_active = max(peak_active, active_count)
            if queued and len(running) >= cap:
                release_id = next((identifier for identifier in running if identifier in verified and identifier != crash_id and identifier not in released), None)
                if release_id is not None:
                    client.call("POST", f"/api/sandboxes/{release_id}/destroy")
                    released.add(release_id)
                    print("PASS  Phase 2: destroying a verified job released the next FIFO item")
            if len(verified) == len(submitted):
                break
            if time.monotonic() >= deadline:
                raise DemoError("three Phase 2 sandboxes did not become healthy within 30 minutes")
            time.sleep(0.5)

        running_after_queue = [identifier for identifier in identifiers if _item(client, identifier).get("status") == "running"]
        if peak_active < expected_peak:
            raise DemoError(f"scheduler reached only {peak_active} concurrent demo jobs; expected {expected_peak} for host cap {cap}")
        if not running_after_queue:
            raise DemoError("no running sandbox remained for stop/restart and crash checks")
        restart_id = running_after_queue[-1]
        stopped = client.call("POST", f"/api/sandboxes/{restart_id}/stop").get("item", {})
        if stopped.get("status") != "stopped":
            raise DemoError("Phase 2 stop did not release the sandbox")
        restarted = client.call("POST", f"/api/sandboxes/{restart_id}/restart").get("item", {})
        if restarted.get("status") not in {"queued", "cloning", "detecting", "building", "starting", "running"}:
            raise DemoError("Phase 2 restart did not queue or start the sandbox")
        _wait_for_running(client, restart_id)
        wait_for_agent(client, restart_id)
        print("PASS  Phase 2: stop and restart preserved agent supervision")
    finally:
        for identifier in identifiers:
            try:
                item = _item(client, identifier)
                if item.get("status") == "queued":
                    client.call("POST", f"/api/sandboxes/{identifier}/cancel")
                elif item.get("status") not in {"destroyed", "failed", "stopped"}:
                    client.call("POST", f"/api/sandboxes/{identifier}/destroy")
                elif item.get("status") in {"failed", "stopped"}:
                    client.call("POST", f"/api/sandboxes/{identifier}/destroy")
            except Exception as exc:
                failures.append(f"cleanup {identifier}: {type(exc).__name__}")
    if failures:
        raise DemoError("Phase 2 cleanup failed: " + "; ".join(failures))
    print(f"PHASE2 PASS: {len(submitted)} repositories, peak concurrency {peak_active}/{cap}, FIFO scheduling, recent agent heartbeats, app proxy, and stop/restart cleanup.")
    return {"peak_active": peak_active, "cap": cap}


def main() -> int:
    base_url = os.environ.get("SANDBOX_MESH_URL", demo_module.BASE).rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        print("FAIL  Phase 2 demo: SANDBOX_MESH_URL must be an HTTP or HTTPS URL", file=sys.stderr)
        return 2
    demo_module.BASE = base_url
    token = os.environ.get("SANDBOX_MESH_TOKEN", "")
    if len(token) < 32:
        print("FAIL  Phase 2 demo: SANDBOX_MESH_TOKEN is not available in this shell", file=sys.stderr)
        return 2
    try:
        client = Client(token)
        print(f"Phase 2 acceptance target: {base_url}")
        client.login()
        result = run_phase2(client)
        from .demo import update_phase2_report
        update_phase2_report(f"passed: {len(ACCEPTANCE_REPOS)} repositories, peak concurrency {result['peak_active']}/{result['cap']}, FIFO scheduling, agent heartbeats, app proxy, and lifecycle checks")
        return 0
    except Exception as exc:
        print(f"PHASE2 FAIL: {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

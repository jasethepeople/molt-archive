#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import platform
import pwd
import re
import shutil
import socket
import subprocess
import sys
import time


VERSION = "0.1.0"
PROBE_SCRIPT = r'''import json, os, pathlib, sys
routes = pathlib.Path("/proc/net/route")
rows = [r for r in routes.read_text().splitlines()[1:] if r.strip()] if routes.exists() else []
uid = os.geteuid()
caps = pathlib.Path("/proc/self/status").read_text()
cap_eff = next((line.split()[1] for line in caps.splitlines() if line.startswith("CapEff:")), "unknown")
print(json.dumps({"uid": uid, "cap_eff": cap_eff, "route_count": len(rows)}))'''
ULIMIT_SCRIPT = "ulimit -t 2 && ulimit -v 131072 && ulimit -u 64 && ulimit -f 10240 && ulimit -n 64 && :"


def run(argv: list[str], timeout: float = 3.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, text=True, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 124, "", str(exc))


def executable(name: str) -> str | None:
    return shutil.which(name)


def parse_limit(path: str) -> int | None:
    try:
        value = pathlib.Path(path).read_text().strip()
        if value in {"max", "-1"}:
            return None
        number = int(value)
        if number >= (1 << 60):
            return None
        return number
    except (OSError, ValueError):
        return None


def cgroup_info() -> dict:
    version = 2 if pathlib.Path("/sys/fs/cgroup/cgroup.controllers").exists() else 1
    controllers = []
    try:
        controllers = pathlib.Path("/sys/fs/cgroup/cgroup.controllers").read_text().split()
    except OSError:
        pass
    cpu_quota = parse_limit("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    cpu_period = parse_limit("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    cpu_max = None
    try:
        quota, period = pathlib.Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            cpu_max = float(quota) / int(period)
    except (OSError, ValueError, IndexError):
        pass
    cpu_v1 = cpu_quota / cpu_period if cpu_quota and cpu_quota > 0 and cpu_period else None
    memory_limit = parse_limit("/sys/fs/cgroup/memory.max")
    if memory_limit is None:
        memory_limit = parse_limit("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    memory_current = parse_limit("/sys/fs/cgroup/memory.current")
    if memory_current is None:
        memory_current = parse_limit("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    pids_limit = parse_limit("/sys/fs/cgroup/pids.max")
    if pids_limit is None:
        pids_limit = parse_limit("/sys/fs/cgroup/pids/pids.max")
    return {
        "version": version,
        "controllers": controllers,
        "cpu_quota_cores": cpu_max if cpu_max is not None else cpu_v1,
        "memory_limit_bytes": memory_limit,
        "memory_current_bytes": memory_current,
        "pids_limit": pids_limit,
        "v2_controllers_writable": bool(controllers) and os.access("/sys/fs/cgroup", os.W_OK),
    }


def check_engine(name: str) -> dict:
    path = executable(name)
    if not path:
        return {"installed": False, "ready": False}
    if name == "docker":
        info = run([path, "info", "--format", "{{json .Runtimes}}"], timeout=5)
        version = run([path, "--version"], timeout=2)
        runtimes = {}
        if info.returncode == 0:
            try:
                runtimes = json.loads(info.stdout.strip() or "{}")
            except json.JSONDecodeError:
                pass
        return {
            "installed": True,
            "ready": info.returncode == 0,
            "version": version.stdout.strip() if version.returncode == 0 else None,
            "gvisor_runtime_configured": "runsc" in runtimes,
            "runtimes": sorted(runtimes),
            "detail": None if info.returncode == 0 else "daemon unavailable or inaccessible",
        }
    info = run([path, "info", "--format", "json"], timeout=5)
    version = run([path, "--version"], timeout=2)
    rootless = False
    runtime_name = None
    if info.returncode == 0:
        try:
            data = json.loads(info.stdout)
            host = data.get("host", {})
            security = host.get("security", {}) if isinstance(host, dict) else {}
            rootless = bool(security.get("rootless"))
            runtime_name = host.get("ociRuntime", {}).get("name") if isinstance(host, dict) else None
        except (json.JSONDecodeError, AttributeError):
            pass
    return {
        "installed": True,
        "ready": info.returncode == 0,
        "rootless": rootless,
        "version": version.stdout.strip() if version.returncode == 0 else None,
        "gvisor_runtime_configured": runtime_name == "runsc",
        "detail": None if info.returncode == 0 else "engine unavailable or inaccessible",
    }


def test_fallback() -> dict:
    unshare = executable("unshare")
    bash = executable("bash")
    if not unshare or not bash or not executable("python3"):
        return {"network_namespace": False, "ulimits": False, "ready": False, "detail": "unshare, bash, or Python is missing"}
    prefix: list[str] = []
    expected_uid = os.geteuid()
    if expected_uid == 0:
        try:
            nobody = pwd.getpwnam("nobody")
        except KeyError:
            return {"network_namespace": False, "ulimits": False, "ready": False, "detail": "no unprivileged nobody account available for probe"}
        runuser = executable("runuser")
        if not runuser:
            return {"network_namespace": False, "ulimits": False, "ready": False, "detail": "cannot safely probe as a non-root account"}
        prefix = [runuser, "-u", nobody.pw_name, "--"]
        expected_uid = nobody.pw_uid
    net = run(prefix + [unshare, "--user", "--map-current-user", "--net", "--fork", "--", executable("python3") or "python3", "-c", PROBE_SCRIPT], timeout=4)
    net_data = {}
    if net.returncode == 0:
        try:
            net_data = json.loads(net.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            pass
    net_ok = net.returncode == 0 and net_data.get("uid") == expected_uid and net_data.get("cap_eff") == "0000000000000000" and net_data.get("route_count") == 0
    limits = run(prefix + [bash, "-c", ULIMIT_SCRIPT], timeout=4)
    limits_ok = limits.returncode == 0
    return {
        "network_namespace": net_ok,
        "ulimits": limits_ok,
        "ready": net_ok and limits_ok,
        "tested_uid": expected_uid,
        "detail": None if net_ok and limits_ok else "non-root network isolation or resource-limit smoke test failed",
    }


def bubblewrap_test() -> dict:
    path = executable("bwrap")
    if not path:
        return {"installed": False, "usable": False}
    args = [path, "--unshare-user", "--unshare-pid", "--unshare-net", "--die-with-parent"]
    if pathlib.Path("/usr").is_dir():
        args += ["--ro-bind", "/usr", "/usr"]
    if pathlib.Path("/lib").exists():
        args += ["--ro-bind", "/lib", "/lib"]
    args += ["--proc", "/proc", "--dev", "/dev", "/usr/bin/true"]
    result = run(args, timeout=4)
    return {"installed": True, "usable": result.returncode == 0, "detail": None if result.returncode == 0 else "namespace/mount smoke test failed"}


def nsjail_test(cgroups: dict) -> dict:
    path = executable("nsjail")
    if not path:
        return {"installed": False, "usable": False}
    help_result = run([path, "--help"], timeout=3)
    usable = cgroups["version"] == 2 and cgroups["v2_controllers_writable"] and help_result.returncode == 0
    return {"installed": True, "usable": usable, "detail": None if usable else "requires a writable delegated cgroup v2 hierarchy"}


def listening_ports() -> list[dict]:
    ss = executable("ss")
    if ss:
        result = run([ss, "-H", "-lntu"], timeout=2)
        if result.returncode == 0:
            found = {}
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) < 5:
                    continue
                proto = fields[0].lower()
                local = fields[4]
                match = re.search(r":(\d+)$", local)
                if match:
                    key = (proto, int(match.group(1)))
                    found[key] = {"protocol": key[0], "port": key[1]}
            return sorted(found.values(), key=lambda item: (item["protocol"], item["port"]))
    found = set()
    for proto, state in (("tcp", "0A"), ("tcp6", "0A"), ("udp", None), ("udp6", None)):
        path = pathlib.Path(f"/proc/net/{proto}")
        try:
            lines = path.read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or (state and fields[3] != state):
                continue
            try:
                port = int(fields[1].rsplit(":", 1)[1], 16)
            except (ValueError, IndexError):
                continue
            if port:
                found.add(("tcp" if proto.startswith("tcp") else "udp", port))
    return [{"protocol": proto, "port": port} for proto, port in sorted(found)]


def available_memory() -> int | None:
    try:
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def driver_choice(capabilities: dict) -> tuple[str, str, bool]:
    docker = capabilities["docker"]
    podman = capabilities["podman"]
    if docker.get("ready"):
        return "docker", "Docker daemon is responding; gVisor is selected when the runsc runtime is configured.", False
    if podman.get("ready") and podman.get("rootless"):
        return "podman-rootless", "Rootless Podman is responding; gVisor is selected when runsc is the active OCI runtime.", False
    nsjail = capabilities["nsjail"]
    if nsjail.get("usable"):
        return "nsjail", "nsjail is installed and a writable cgroup v2 delegation is available.", False
    bwrap = capabilities["bubblewrap"]
    cgroups = capabilities["cgroups"]
    if bwrap.get("usable") and cgroups["version"] == 2 and cgroups["v2_controllers_writable"]:
        return "bubblewrap-cgroup-v2", "bubblewrap namespaces passed a smoke test and cgroup v2 controllers are writable.", False
    fallback = capabilities["fallback3"]
    if fallback.get("ready"):
        return "userns-ulimits-private-netns", "No container/jail driver passed checks; non-root user namespaces, private network namespaces, and shell resource limits work. This is degraded isolation.", True
    return "none", "No configured driver passed its readiness checks; repository execution is disabled.", False


def derive_concurrency_cap(scheduler_config: dict, sandbox_config: dict, host_id: str, effective_cpus: float, available_memory: int | None) -> int:
    default_cap = max(1, int(scheduler_config.get("default_concurrency_cap", 1)))
    maximum_cap = max(1, int(scheduler_config.get("max_concurrency_cap", default_cap)))
    host_cap = max(1, int(scheduler_config.get("host_concurrency_caps", {}).get(host_id.lower(), default_cap)))
    reserve_memory = max(0, int(scheduler_config.get("capacity_reserve_memory_bytes", 0)))
    per_sandbox_memory = max(1, int(sandbox_config.get("memory_bytes", 1)))
    per_sandbox_cpu = max(0.1, float(sandbox_config.get("cpu_cores", 1)))
    cpu_cap = max(1, int(max(0.1, effective_cpus) / per_sandbox_cpu))
    memory_cap = max(1, (max(0, (available_memory or 0) - reserve_memory) // per_sandbox_memory)) if available_memory is not None else 1
    return min(default_cap, maximum_cap, host_cap, cpu_cap, memory_cap)


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe local sandbox drivers and derive a conservative host profile.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--host-id", default=os.environ.get("SANDBOX_MESH_HOST_ID", socket.gethostname()))
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()
    root = pathlib.Path(args.project_root).resolve()
    output = pathlib.Path(args.output_dir).resolve() if args.output_dir else root
    if not output.is_relative_to(root):
        parser.error("--output-dir must stay inside the sandbox-mesh project directory")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", args.host_id):
        parser.error("--host-id may contain only letters, numbers, dots, underscores, and hyphens")
    output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root))
    from mesh.config import parse_yaml_subset
    try:
        phase_config = parse_yaml_subset((root / "config.yaml").read_text(encoding="utf-8"), "config.yaml")
    except (OSError, ValueError) as exc:
        parser.error(f"cannot read scheduler limits from config.yaml ({type(exc).__name__})")
    scheduler_config = phase_config.get("scheduler", {})
    sandbox_config = phase_config.get("sandbox", {})

    cgroups = cgroup_info()
    docker = check_engine("docker")
    podman = check_engine("podman")
    cpu_count = os.cpu_count() or 1
    cpu_quota = cgroups.get("cpu_quota_cores")
    effective_cpus = max(0.1, min(float(cpu_count), cpu_quota)) if cpu_quota else float(cpu_count)
    memory_limit = cgroups.get("memory_limit_bytes")
    memory_available = available_memory()
    if memory_limit:
        current = cgroups.get("memory_current_bytes") or 0
        cgroup_available = max(0, memory_limit - current)
        memory_available = min(memory_available, cgroup_available) if memory_available is not None else cgroup_available
    try:
        disk = shutil.disk_usage(root)
        disk_info = {"total_bytes": disk.total, "free_bytes": disk.free}
    except OSError:
        disk_info = {"total_bytes": None, "free_bytes": None}
    default_cap = int(scheduler_config.get("default_concurrency_cap", 1))
    maximum_cap = int(scheduler_config.get("max_concurrency_cap", default_cap))
    concurrency = derive_concurrency_cap(scheduler_config, sandbox_config, args.host_id, effective_cpus, memory_available)
    fallback = test_fallback()
    caps = {
        "docker": docker,
        "podman": podman,
        "bubblewrap": bubblewrap_test(),
        "nsjail": nsjail_test(cgroups),
        "firejail": {"installed": bool(executable("firejail"))},
        "runsc": {"installed": bool(executable("runsc"))},
        "kvm": {"available": pathlib.Path("/dev/kvm").exists()},
        "user_namespaces": {"tested": fallback["network_namespace"], "available": fallback["network_namespace"]},
        "cgroups": cgroups,
        "systemd": {"installed": bool(executable("systemctl")), "active_as_pid1": pathlib.Path("/run/systemd/system").exists()},
        "fallback3": fallback,
    }
    driver, rationale, approval = driver_choice(caps)
    profile = {
        "schema_version": 1,
        "probe_version": VERSION,
        "host_id": args.host_id,
        "observed_hostname": socket.gethostname(),
        "observed_at_epoch": int(time.time()),
        "os": {
            "name": os.uname().sysname,
            "release": os.uname().release,
            "machine": os.uname().machine,
            "distribution": platform.freedesktop_os_release().get("PRETTY_NAME", "unknown"),
        },
        "resources": {
            "online_cpu_count": cpu_count,
            "effective_cpu_cores": round(effective_cpus, 2),
            "available_memory_bytes": memory_available,
            "disk": disk_info,
            "concurrency_cap": concurrency,
            "max_concurrency_cap": maximum_cap,
            "per_sandbox_recommendation": {
                "cpu_cores": round(max(0.1, effective_cpus / max(1, concurrency) * 0.7), 2),
                "memory_bytes": max(256 * 1024**2, int((memory_available or 0) / (concurrency + 1))),
                "pids": 128,
                "disk_bytes": max(512 * 1024**2, int((disk_info["free_bytes"] or 0) * 0.05)),
            },
        },
        "capabilities": caps,
        "open_ports": listening_ports(),
        "sandbox_driver": driver,
        "driver_rationale": rationale,
        "requires_user_approval_before_untrusted_execution": approval,
        "repository_execution_enabled": False,
        "warnings": (["DEGRADED ISOLATION: option 3 selected. Do not run untrusted repositories until the owner explicitly approves and the isolation limitations are accepted."] if approval else []),
    }
    profile_path = output / "host-profile.json"
    report_path = output / "probe-report.md"
    temporary = output / ".host-profile.json.tmp"
    temporary.write_text(json.dumps(profile, indent=2) + "\n")
    temporary.replace(profile_path)
    report_path.write_text(render_report(profile))
    sys.stdout.write(report_path.read_text())
    return 0


def render_report(profile: dict) -> str:
    caps = profile["capabilities"]
    resources = profile["resources"]
    def status(key: str) -> str:
        entry = caps[key]
        if key in {"docker", "podman"}:
            return "ready" if entry.get("ready") else ("installed, unavailable" if entry.get("installed") else "not installed")
        if key == "bubblewrap" or key == "nsjail":
            return "usable" if entry.get("usable") else ("installed, unusable" if entry.get("installed") else "not installed")
        return "available" if entry.get("available", entry.get("installed", False)) else "not available"
    ports = ", ".join(f"{item['protocol']}/{item['port']}" for item in profile["open_ports"]) or "none detected"
    free_memory = resources["available_memory_bytes"]
    memory_text = f"{free_memory / 1024**3:.2f} GiB available" if free_memory is not None else "unknown"
    disk_text = f"{resources['disk']['free_bytes'] / 1024**3:.2f} GiB free" if resources["disk"]["free_bytes"] is not None else "unknown"
    lines = [
        f"# Sandbox Mesh capability probe — {profile['host_id']}",
        "",
        f"- Observed hostname: `{profile['observed_hostname']}`",
        f"- OS/kernel: {profile['os']['distribution']}; `{profile['os']['name']} {profile['os']['release']}` ({profile['os']['machine']})",
        f"- CPU: {resources['effective_cpu_cores']:.2f} effective cores ({resources['online_cpu_count']} online); memory: {memory_text}; disk: {disk_text}",
        f"- Derived concurrency cap: **{resources['concurrency_cap']}**",
        f"- Open/listening ports: {ports}",
        "",
        "| Capability | Result |",
        "|---|---|",
        f"| Docker | {status('docker')} |",
        f"| Rootless Podman | {status('podman')} (rootless={caps['podman'].get('rootless', False)}) |",
        f"| bubblewrap | {status('bubblewrap')} |",
        f"| nsjail | {status('nsjail')} |",
        f"| firejail | {status('firejail')} |",
        f"| gVisor/runsc | {'installed' if caps['runsc']['installed'] else 'not installed'} |",
        f"| KVM | {'available' if caps['kvm']['available'] else 'not available'} |",
        f"| User + private network namespaces | {'tested successfully' if caps['user_namespaces']['tested'] else 'not verified'} |",
        f"| cgroups | v{caps['cgroups']['version']} (writable delegated v2={caps['cgroups']['v2_controllers_writable']}) |",
        f"| systemd | {'active' if caps['systemd']['active_as_pid1'] else 'not active'} |",
        "",
        f"**Selected driver:** `{profile['sandbox_driver']}` — {profile['driver_rationale']}",
        "",
        f"**Repository execution:** {'requires explicit owner approval first' if profile['requires_user_approval_before_untrusted_execution'] else ('disabled: no usable driver' if profile['sandbox_driver'] == 'none' else 'not enabled by this Phase 0 package')}",
        "",
    ]
    for warning in profile["warnings"]:
        lines.extend([f"> **Warning:** {warning}", ""])
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())

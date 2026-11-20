from __future__ import annotations

import ast
import ipaddress
import json
import os
import pathlib
import platform
import re
import shlex
import socket


class ConfigError(ValueError):
    pass


_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_HOST = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*$")


def _strip_comment(value: str) -> str:
    quote = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif char == "\\" and quote == '"':
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.rstrip()


def _split_mapping(value: str) -> tuple[str, str] | None:
    quote = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif char == "\\" and quote == '"':
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == ":":
            key = value[:index].strip()
            if _KEY.fullmatch(key):
                return key, value[index + 1:].strip()
    return None


def _scalar(raw: str):
    text = raw.strip()
    if not text:
        return None
    if text.startswith(("[", "{")):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"only JSON-compatible inline YAML lists/maps are supported: {text[:80]}") from exc
    if text.startswith('"') and text.endswith('"'):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError("invalid double-quoted YAML string") from exc
    if text.startswith("'") and text.endswith("'"):
        try:
            return ast.literal_eval(text)
        except (ValueError, SyntaxError) as exc:
            raise ConfigError("invalid single-quoted YAML string") from exc
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "~"}:
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    if text[0] in "&*!|>":
        raise ConfigError("YAML anchors, tags, aliases, and block scalars are not supported")
    return text


def parse_yaml_subset(text: str, source: str = "YAML") -> dict:
    tokens: list[tuple[int, str, int]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw:
            raise ConfigError(f"{source}:{number}: tabs are not allowed in indentation")
        line = _strip_comment(raw.lstrip(" "))
        if not line.strip():
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        tokens.append((indent, line.strip(), number))
    if not tokens:
        return {}

    def parse_block(index: int, indent: int):
        if index >= len(tokens) or tokens[index][0] != indent:
            raise ConfigError(f"{source}: expected a YAML block at indentation {indent}")
        is_sequence = tokens[index][1] == "-" or tokens[index][1].startswith("- ")
        result = [] if is_sequence else {}
        while index < len(tokens) and tokens[index][0] == indent:
            _, body, number = tokens[index]
            if is_sequence:
                if not (body == "-" or body.startswith("- ")):
                    break
                rest = body[1:].strip()
                index += 1
                if not rest:
                    if index >= len(tokens) or tokens[index][0] <= indent:
                        result.append(None)
                    else:
                        item, index = parse_block(index, tokens[index][0])
                        result.append(item)
                    continue
                mapping = _split_mapping(rest)
                if mapping:
                    key, raw_value = mapping
                    item = {}
                    if raw_value:
                        item[key] = _scalar(raw_value)
                    elif index < len(tokens) and tokens[index][0] > indent + 1:
                        child, index = parse_block(index, tokens[index][0])
                        item[key] = child
                    else:
                        item[key] = {}
                    if index < len(tokens) and tokens[index][0] > indent:
                        child_indent = tokens[index][0]
                        if child_indent <= indent:
                            raise ConfigError(f"{source}:{tokens[index][2]}: invalid sequence mapping indentation")
                        child, index = parse_block(index, child_indent)
                        if not isinstance(child, dict):
                            raise ConfigError(f"{source}:{tokens[index - 1][2]}: sequence mapping continuation must be a map")
                        for child_key, child_value in child.items():
                            if child_key in item:
                                raise ConfigError(f"{source}:{number}: repeated mapping key {child_key}")
                            item[child_key] = child_value
                    result.append(item)
                else:
                    result.append(_scalar(rest))
                    if index < len(tokens) and tokens[index][0] > indent:
                        raise ConfigError(f"{source}:{tokens[index][2]}: scalar list items cannot have nested values")
            else:
                pair = _split_mapping(body)
                if not pair:
                    raise ConfigError(f"{source}:{number}: expected a mapping key and value")
                key, raw_value = pair
                if key in result:
                    raise ConfigError(f"{source}:{number}: repeated key {key}")
                index += 1
                if raw_value:
                    result[key] = _scalar(raw_value)
                    if index < len(tokens) and tokens[index][0] > indent:
                        raise ConfigError(f"{source}:{tokens[index][2]}: scalar values cannot have nested values")
                elif index < len(tokens) and tokens[index][0] > indent:
                    child, index = parse_block(index, tokens[index][0])
                    result[key] = child
                else:
                    result[key] = {}
        return result, index

    if tokens[0][0] != 0:
        raise ConfigError(f"{source}:{tokens[0][2]}: document must start at indentation 0")
    document, end = parse_block(0, 0)
    if end != len(tokens) or not isinstance(document, dict):
        raise ConfigError(f"{source}: document must be a single top-level mapping")
    return document


def load_config(project_root: pathlib.Path) -> dict:
    path = project_root / "config.yaml"
    try:
        config = parse_yaml_subset(path.read_text(encoding="utf-8"), "config.yaml")
    except OSError as exc:
        raise ConfigError("config.yaml could not be read") from exc
    validate_config(config)
    config["runtime"] = runtime_profile(config, project_root)
    return config


def runtime_profile(config: dict, project_root: pathlib.Path) -> dict:
    profile = {}
    profile_path = project_root / "host-profile.json"
    try:
        loaded = json.loads(profile_path.read_text(encoding="utf-8"))
        profile = loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
        profile = {}
    configured_host = os.environ.get("SANDBOX_MESH_HOST_ID", "").strip().lower()
    if not configured_host:
        configured_host = str(profile.get("host_id") or "").strip().lower()
    if not configured_host or configured_host == "auto":
        hostname = socket.gethostname().lower()
        configured_host = "foundry" if hostname == "foundry" else "sandbox"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", configured_host):
        raise ConfigError("runtime host id is invalid")
    if configured_host == "foundry" and profile.get("host_id") != "foundry":
        try:
            loaded = json.loads((project_root / "host-profile-foundry.json").read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("host_id") == "foundry":
                profile = loaded
        except (OSError, json.JSONDecodeError):
            pass
    resources = profile.get("resources", {}) if isinstance(profile.get("resources", {}), dict) else {}
    affinity_count = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    cpu_count = resources.get("online_cpu_count", affinity_count)
    try:
        cpu_count = max(1, min(256, int(cpu_count)))
    except (TypeError, ValueError):
        cpu_count = max(1, min(256, os.cpu_count() or 1))
    effective_cpus = resources.get("effective_cpu_cores", cpu_count)
    try:
        effective_cpus = max(0.1, min(float(effective_cpus), 256.0))
    except (TypeError, ValueError):
        effective_cpus = float(cpu_count)
    available_memory = resources.get("available_memory_bytes")
    try:
        available_memory = int(available_memory)
    except (TypeError, ValueError):
        available_memory = _available_memory_bytes()
    scheduler = config["scheduler"]
    host_cap = int(scheduler["host_concurrency_caps"].get(configured_host, scheduler["default_concurrency_cap"]))
    maximum_cap = int(scheduler["max_concurrency_cap"])
    reserved_memory = int(scheduler["capacity_reserve_memory_bytes"])
    per_job_memory = int(config["sandbox"]["memory_bytes"])
    memory_cap = max(1, (max(0, available_memory - reserved_memory) // per_job_memory)) if available_memory is not None else 1
    measured_cap = resources.get("concurrency_cap")
    cpu_cap = max(1, int(effective_cpus / float(config["sandbox"]["cpu_cores"])))
    caps = [host_cap, maximum_cap, cpu_cap, memory_cap]
    if isinstance(measured_cap, int) and measured_cap > 0:
        caps.append(measured_cap)
    return {
        "host_id": configured_host,
        "cpu_count": cpu_count,
        "effective_cpu_cores": float(effective_cpus),
        "available_memory_bytes": available_memory,
        "machine": platform.machine().lower(),
        "concurrency_cap": max(1, min(caps)),
    }


def _available_memory_bytes() -> int | None:
    try:
        for line in pathlib.Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, separator, value = line.partition(":")
            if separator and key == "MemAvailable":
                fields = value.split()
                return int(fields[0]) * (1024 if len(fields) > 1 and fields[1] == "kB" else 1)
    except (OSError, UnicodeError, ValueError, IndexError):
        return None
    return None


def validate_config(config: dict) -> None:
    require(config.get("schema_version") == 1 and config.get("phase") == 2, "config.yaml must set schema_version: 1 and phase: 2")
    require(config.get("host_id") in {"auto", "foundry"} and config.get("driver") == "docker", "this build uses the measured Docker host profile")
    features = config.get("features", {})
    require(features.get("phase_1") is True and features.get("phase_2") is True and all(features.get(f"phase_{phase}") is False for phase in range(3, 6)), "Phases 1 and 2 must be enabled and Phases 3–5 must remain disabled")
    control = config.get("control_plane", {})
    require(control.get("bind_host") == "127.0.0.1" and control.get("port") == 8787 and control.get("authentication_required") is True, "the authenticated control plane must bind only to 127.0.0.1:8787")
    require(1 <= int(control.get("session_ttl_seconds", 0)) <= 86400, "session TTL must be at most 24 hours")
    require(1024 <= int(control.get("max_request_bytes", 0)) <= 32768, "request body limit must be from 1 KiB to 32 KiB")
    require(config.get("resources", {}).get("concurrency_cap") == 1, "Foundry's measured Phase 1 concurrency cap is exactly 1")
    scheduler = config.get("scheduler", {})
    maximum_cap = scheduler.get("max_concurrency_cap")
    default_cap = scheduler.get("default_concurrency_cap")
    require(isinstance(maximum_cap, int) and maximum_cap >= 1, "scheduler.max_concurrency_cap must be a positive integer")
    require(isinstance(default_cap, int) and 1 <= default_cap <= maximum_cap, "scheduler.default_concurrency_cap must be within the configured maximum")
    host_caps = scheduler.get("host_concurrency_caps", {})
    require(isinstance(host_caps, dict) and host_caps.get("foundry") == 1 and all(isinstance(value, int) and 1 <= value <= maximum_cap for value in host_caps.values()), "Foundry's measured scheduler cap must be 1 and host caps must be within the configured maximum")
    guard = scheduler.get("host_guard", {})
    require(float(guard.get("max_1m_load_per_cpu", 0)) > 0 and int(guard.get("min_available_memory_bytes", 0)) >= 1, "scheduler host guard thresholds are required")
    require(int(scheduler.get("capacity_reserve_memory_bytes", 0)) >= 1, "scheduler memory reserve is required")
    require(0.1 <= float(scheduler.get("retry_interval_seconds", 0)) <= 30, "scheduler retry interval must be from 0.1 to 30 seconds")
    agent = config.get("agent", {})
    require(int(agent.get("heartbeat_interval_seconds", 0)) == 5 and int(agent.get("silent_after_seconds", 0)) == 15, "Phase 2 agent heartbeat timing must be 5 seconds and silent after 15 seconds")
    require(isinstance(agent.get("supervisor_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", agent["supervisor_sha256"]) is not None, "agent.supervisor_sha256 must pin the ARM64 supervisor binary")
    sandbox = config.get("sandbox", {})
    require(sandbox.get("allow_untrusted_execution") is True and int(sandbox.get("default_ttl_seconds", 0)) == 1800, "Phase 1 execution must be enabled behind Docker with a 30-minute TTL")
    require(sandbox.get("platform") == "linux/arm64", "sandbox platform must be linux/arm64")
    require(0 < float(sandbox.get("cpu_cores", 0)) <= 0.7, "sandbox CPU allocation exceeds the measured Foundry budget")
    require(0 < int(sandbox.get("memory_bytes", 0)) <= 1_610_612_736, "sandbox memory allocation exceeds the conservative Foundry budget")
    require(1 <= int(sandbox.get("pids_limit", 0)) <= 128, "sandbox PID limit must be at most 128")
    require(1 <= int(sandbox.get("tmpfs_bytes", 0)) <= 67_108_864, "sandbox tmpfs must be no larger than 64 MiB")
    require(sandbox.get("port_range") == [8900, 8999], "sandbox port range must remain exactly 8900–8999")
    require(str(sandbox.get("user")) not in {"0", "0:0", "root"}, "sandbox must run as a non-root user")
    require(60 <= int(sandbox.get("build_timeout_seconds", 0)) <= 3600, "build timeout must be between 60 seconds and one hour")
    require(30 <= int(sandbox.get("healthcheck_timeout_seconds", 0)) <= 300, "sandbox health-check timeout is invalid")
    require(1 <= int(sandbox.get("max_checkout_bytes", 0)) <= 2_000_000_000, "repository checkout limit is invalid")
    require(1 <= int(sandbox.get("clone_cache_entries", 0)) <= 8, "repository cache must retain no more than 8 commits")
    require(1 <= int(sandbox.get("image_cache_entries", 0)) <= 3, "image cache must retain no more than 3 sandbox images")
    require(1 <= int(sandbox.get("max_image_bytes", 0)) <= 2_147_483_648, "sandbox image size must be capped at 2 GiB")
    egress = config.get("egress", {})
    require(egress.get("default_deny") is True, "egress default-deny cannot be disabled")
    hosts = egress.get("allowlist", [])
    require(isinstance(hosts, list) and len(hosts) <= 100, "egress.allowlist must contain no more than 100 exact DNS names")
    normalized = [host.lower().rstrip(".") for host in hosts if isinstance(host, str)]
    require(len(normalized) == len(hosts) and all(_HOST.fullmatch(host) for host in normalized), "egress.allowlist must contain exact DNS names, not wildcards or IP addresses")
    for host in normalized:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            continue
        raise ConfigError("egress.allowlist must contain DNS names, not IP addresses")
    require(len(set(normalized)) == len(normalized), "egress.allowlist cannot contain duplicates")
    require(egress.get("proxy_image") == "sandbox-mesh/egress-proxy:1", "egress proxy image name is reserved for the built-in proxy")
    require(egress.get("ingress_port") == 8081, "edge proxy container port is reserved for the built-in ingress proxy")
    build = config.get("build", {})
    require(build.get("buildx_version") == "0.39.0" and build.get("buildx_sha256") == "722cdcdfdd6f3fb943fa1ba75102d057feb18ba72aab93b1b0a8cf2b2b48a25f", "Buildx must use the pinned ARM64 binary checksum")
    require(build.get("engine") == "nixpacks" and build.get("nixpacks_version") == "1.41.0", "non-Dockerfile repositories must use the pinned Nixpacks release")
    require(build.get("nixpacks_sha256") == "912bd02dd2bb6f9c3a9ed965fe8a68b4aa318dc7a2546e2eca6f2806a894ba39", "Nixpacks must use the verified ARM64 release checksum")
    require(build.get("nixpacks_base_image") == "ghcr.io/railwayapp/nixpacks:ubuntu-1745885067", "Nixpacks base image must match the pinned ARM64 plan")
    require(build.get("buildkit_version") == "0.35.0" and build.get("buildkit_image") == "moby/buildkit:v0.35.0", "BuildKit must use the pinned resource-limit-capable ARM64 image")
    require(build.get("buildkit_container") == "buildx_buildkit_sandbox-mesh-builder0" and build.get("buildkit_builder") == "sandbox-mesh-builder", "the isolated BuildKit builder name is reserved")
    require(0 < float(build.get("buildkit_cpu_cores", 0)) <= 0.7, "BuildKit CPU limit exceeds the measured Foundry budget")
    require(0 < int(build.get("buildkit_memory_bytes", 0)) <= 1_610_612_736, "BuildKit memory limit exceeds the conservative Foundry budget")
    require(int(build.get("buildkit_pids_limit", 0)) == 128 and int(build.get("buildkit_max_parallelism", 0)) == 1, "BuildKit must enforce the measured process and concurrency caps")
    warm = config.get("warm_pool", {})
    require(warm.get("enabled") is True and warm.get("image") == "node:22-alpine", "Phase 1 requires exactly one Node warm base container")
    require(config.get("demo", {}).get("fixtures_enabled") is True, "the bounded local acceptance fixtures must remain available")


def load_sandbox_overrides(repo_root: pathlib.Path) -> dict:
    root = repo_root.resolve(strict=True)
    path = repo_root / "sandbox.yaml"
    if not path.exists():
        return {}
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ConfigError("sandbox.yaml must be a regular file inside the repository") from exc
    if not resolved.is_file() or resolved.stat().st_size > 16_384:
        raise ConfigError("sandbox.yaml must be a regular file no larger than 16 KiB")
    values = parse_yaml_subset(resolved.read_text(encoding="utf-8"), "sandbox.yaml")
    extra = set(values) - {"build", "run", "port"}
    if extra:
        raise ConfigError("sandbox.yaml may override build, run, and port only; isolation, resource limits, mounts, and egress remain fixed")
    result: dict = {}
    if "build" in values:
        value = values["build"]
        if isinstance(value, str):
            result["build"] = [parse_command(value, "build")]
        elif isinstance(value, list) and value and all(isinstance(command, str) for command in value):
            result["build"] = [parse_command(value, "build")]
        elif isinstance(value, list) and value and all(isinstance(command, list) for command in value):
            result["build"] = [parse_command(command, "build") for command in value]
        else:
            raise ConfigError("sandbox.yaml build must be a command string or argv list")
    if "run" in values:
        result["run"] = parse_command(values["run"], "run")
    if "port" in values:
        port = values["port"]
        if isinstance(port, str) and port.isdigit():
            port = int(port)
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConfigError("sandbox.yaml port must be an integer from 1 to 65535")
        result["port"] = port
    return result


def parse_command(value, name: str) -> list[str]:
    if isinstance(value, str):
        try:
            command = shlex.split(value, posix=True)
        except ValueError as exc:
            raise ConfigError(f"sandbox.yaml {name} command has invalid quoting") from exc
    elif isinstance(value, list) and all(isinstance(part, str) for part in value):
        command = value
    else:
        raise ConfigError(f"sandbox.yaml {name} must be a command string or argv list")
    if not command or len(command) > 64 or any(not part or "\x00" in part or len(part) > 4096 for part in command):
        raise ConfigError(f"sandbox.yaml {name} command is empty or too large")
    return command


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)

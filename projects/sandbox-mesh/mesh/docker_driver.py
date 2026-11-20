from __future__ import annotations

import json
import hashlib
import ipaddress
import os
import pathlib
import queue
import re
import select
import secrets
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Callable

from .driver import DriverError


class DockerDriver:
    def __init__(self, project_root: pathlib.Path, config: dict):
        self.root = project_root.resolve()
        self.config = config
        self.sandbox = config["sandbox"]
        self.egress = config["egress"]
        self.build_config = config["build"]
        self._docker_config = self.root / "data" / "docker-config"
        self._docker_config.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._docker = ["docker", "--config", str(self._docker_config), "--host", "unix:///var/run/docker.sock"]
        self._nixpacks = self.root / ".tools" / "nixpacks"
        self._buildx_cli = [*self._docker, "buildx"]
        self._env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": str(self.root / "data" / "docker-home"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "DOCKER_HOST": "unix:///var/run/docker.sock",
            "DOCKER_CONFIG": str(self._docker_config),
            "DOCKER_BUILDKIT": "1",
            "DOCKER_CLI_HINTS": "false",
            "PYTHONUNBUFFERED": "1",
        }
        (self.root / "data" / "docker-home").mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / "tmp").mkdir(parents=True, exist_ok=True, mode=0o700)

    def _redact(self, text: str) -> str:
        for key in ("SANDBOX_MESH_TOKEN", "GITHUB_TOKEN"):
            secret = os.environ.get(key)
            if secret:
                text = text.replace(secret, "[redacted]")
        return text

    def _command(self, argv: list[str], timeout: int = 300, on_output: Callable[[str], None] | None = None, cwd: pathlib.Path | None = None) -> str:
        try:
            process = subprocess.Popen(argv, cwd=cwd or self.root, env=self._env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, close_fds=True)
        except OSError as exc:
            raise DriverError("required Docker or Buildpacks command could not be started") from exc
        output: list[str] = []
        messages: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            try:
                if process.stdout is not None:
                    for line in process.stdout:
                        messages.put(line)
            finally:
                messages.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        ended = False
        try:
            while not ended or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    raise DriverError(f"isolated build step exceeded its {timeout}-second timeout")
                try:
                    line = messages.get(timeout=min(0.25, remaining))
                except queue.Empty:
                    continue
                if line is None:
                    ended = True
                    continue
                safe = self._redact(line.rstrip("\n"))
                output.append(safe)
                if on_output:
                    on_output(safe)
            return_code = process.wait(timeout=2)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise DriverError("Docker command did not stop after its timeout") from exc
        finally:
            if process.poll() is None:
                process.kill()
            if process.stdout:
                process.stdout.close()
        if return_code:
            detail = "\n".join(output[-30:])[-3000:]
            raise DriverError(f"isolated build command exited with status {return_code}" + (f": {detail}" if detail else ""))
        return "\n".join(output)

    def _capture(self, argv: list[str], timeout: int = 30) -> str:
        try:
            result = subprocess.run(argv, env=self._env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False, close_fds=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DriverError("Docker command failed or timed out") from exc
        if result.returncode:
            detail = self._redact((result.stderr or result.stdout).strip()[-1600:])
            raise DriverError("Docker command failed" + (f": {detail}" if detail else ""))
        return result.stdout.strip()

    def _ensure_buildkit(self) -> None:
        build = self.build_config
        try:
            version = self._capture([*self._buildx_cli, "version"], timeout=15)
            if build["buildx_version"] not in version:
                raise DriverError("the project-pinned Buildx plugin is missing or has the wrong version")
            nixpacks = subprocess.run([str(self._nixpacks), "--version"], env=self._env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False, close_fds=True)
            if nixpacks.returncode or build["nixpacks_version"] not in (nixpacks.stdout + nixpacks.stderr):
                raise DriverError("the project-pinned Nixpacks binary is missing or has the wrong version")
            self._capture([*self._buildx_cli, "inspect", build["buildkit_builder"], "--bootstrap"], timeout=120)
            container = build["buildkit_container"]
            host_config = json.loads(self._capture([*self._docker, "inspect", "--format", "{{json .HostConfig}}", container], timeout=20))
            expected_memory = int(build["buildkit_memory_bytes"])
            expected_period = 100000
            expected_quota = int(round(float(build["buildkit_cpu_cores"]) * expected_period))
            expected_pids = int(build["buildkit_pids_limit"])
            if (host_config.get("Memory"), host_config.get("CpuPeriod"), host_config.get("CpuQuota"), host_config.get("PidsLimit")) != (expected_memory, expected_period, expected_quota, expected_pids):
                self._command([*self._docker, "update", "--memory", str(expected_memory), "--memory-swap", str(expected_memory), "--cpu-period", str(expected_period), "--cpu-quota", str(expected_quota), "--pids-limit", str(expected_pids), container], timeout=30)
                host_config = json.loads(self._capture([*self._docker, "inspect", "--format", "{{json .HostConfig}}", container], timeout=20))
            if (host_config.get("Memory"), host_config.get("CpuPeriod"), host_config.get("CpuQuota"), host_config.get("PidsLimit")) != (expected_memory, expected_period, expected_quota, expected_pids):
                raise DriverError("BuildKit CPU, memory, or PID limits did not verify")
            networks = json.loads(self._capture([*self._docker, "inspect", "--format", "{{json .NetworkSettings.Networks}}", container], timeout=20))
            for name in list(networks):
                if name == "bridge" or name.startswith("sm-net-"):
                    self._command([*self._docker, "network", "disconnect", "--force", name, container], timeout=20)
                else:
                    raise DriverError("BuildKit is attached to an unrecognized network; refusing repository execution")
            remaining = json.loads(self._capture([*self._docker, "inspect", "--format", "{{json .NetworkSettings.Networks}}", container], timeout=20))
            if remaining:
                raise DriverError("BuildKit must have no network while idle")
        except DriverError:
            raise
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise DriverError("the isolated resource-limited BuildKit builder is unavailable") from exc

    def ensure_ready(self) -> dict:
        profile = None
        for profile_path in (self.root / "host-profile.json", self.root / "host-profile-foundry.json"):
            try:
                candidate = json.loads(profile_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if candidate.get("host_id") == "foundry":
                profile = candidate
                break
        if not profile:
            raise DriverError("verified Foundry host profile is missing or invalid")
        if profile.get("sandbox_driver") != "docker" or profile.get("resources", {}).get("concurrency_cap") != 1:
            raise DriverError("host profile does not confirm Foundry's Docker driver and concurrency cap 1")
        if profile.get("os", {}).get("machine") not in {"aarch64", "arm64"}:
            raise DriverError("host profile does not confirm the measured ARM64 architecture")
        try:
            values = self._capture([*self._docker, "info", "--format", "{{.OSType}}|{{.Architecture}}|{{.CgroupVersion}}|{{.DefaultRuntime}}"], timeout=20).split("|")
        except DriverError as exc:
            raise DriverError("Docker is unavailable; this host has no fallback driver and will refuse repository execution") from exc
        if len(values) != 4 or values[0] != "linux" or values[1] not in {"aarch64", "arm64"} or values[2] != "2" or values[3] != "runc":
            raise DriverError("Docker must be Linux ARM64 on cgroups v2 with runc; no weaker driver fallback is permitted")
        self._ensure_buildkit()
        return {"engine": "docker", "architecture": "arm64", "cgroups": "v2", "runtime": "runc"}

    def _image_architecture(self, image: str) -> str | None:
        try:
            return self._capture([*self._docker, "image", "inspect", "--format", "{{.Architecture}}", image], timeout=20)
        except DriverError:
            return None

    def _check_image_size(self, image: str) -> None:
        metadata = self._image_metadata(image)
        size = int(metadata.get("Size", 0))
        if size <= 0 or size > int(self.sandbox["max_image_bytes"]):
            self._remove_image(image)
            raise DriverError("sandbox image is empty or exceeds the configured 2 GiB image limit")

    def _image_metadata(self, image: str) -> dict:
        try:
            raw = self._capture([*self._docker, "image", "inspect", "--format", "{{json .}}", image], timeout=20)
            value = json.loads(raw)
        except (DriverError, json.JSONDecodeError) as exc:
            raise DriverError("sandbox image metadata could not be verified") from exc
        if not isinstance(value, dict):
            raise DriverError("sandbox image metadata has an unexpected shape")
        return value

    def _remove_image(self, image: str) -> None:
        result = subprocess.run([*self._docker, "image", "rm", image], env=self._env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30, check=False, close_fds=True)
        detail = (result.stderr or result.stdout).lower()
        if result.returncode and "no such image" not in detail and "not found" not in detail:
            raise DriverError("sandbox image could not be removed")

    def trim_image_cache(self, keep_image: str | None = None) -> None:
        image_ids = set(self._capture([*self._docker, "image", "ls", "-q", "--no-trunc", "--filter", "label=com.sandbox-mesh.managed=true"], timeout=30).splitlines())
        entries = []
        for identifier in image_ids:
            metadata = self._image_metadata(identifier)
            tags = metadata.get("RepoTags") or []
            if not any(tag.startswith("sandbox-mesh/app:") for tag in tags):
                continue
            entries.append((str(metadata.get("Created", "")), identifier, tags))
        entries.sort(key=lambda item: item[0], reverse=True)
        active_ids = set()
        containers = self._capture([*self._docker, "ps", "-q", "--filter", "label=com.sandbox-mesh.role=app"], timeout=20).splitlines()
        for container in containers:
            try:
                active_ids.add(self._capture([*self._docker, "inspect", "--format", "{{.Image}}", container], timeout=10))
            except DriverError:
                continue
        required = int(self.sandbox["image_cache_entries"])
        keep = {identifier for _, identifier, _ in entries[:required]} | active_ids
        if keep_image:
            metadata = self._image_metadata(keep_image)
            keep.add(metadata.get("Id", ""))
        for _, identifier, _ in entries:
            if identifier not in keep:
                self._remove_image(identifier)

    def _image_tag(self, record: dict) -> str:
        sha = record.get("commit_sha") or ""
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise DriverError("repository commit identifier is missing or invalid")
        return f"sandbox-mesh/app:{sha}"

    def _supervisor_tag(self, record: dict) -> str:
        sha = record.get("commit_sha") or ""
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise DriverError("repository commit identifier is missing or invalid")
        binary = self.root / "mesh" / "supervisor" / "bin" / "sandbox-supervisor-linux-arm64"
        try:
            full_digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        except OSError as exc:
            raise DriverError("the pinned ARM64 sandbox supervisor binary is missing") from exc
        if full_digest != self.config["agent"]["supervisor_sha256"]:
            raise DriverError("the ARM64 sandbox supervisor binary does not match its pinned SHA-256")
        return f"sandbox-mesh/supervisor:{sha[:40]}-{full_digest[:12]}"

    def _label_args(self, record: dict, role: str) -> list[str]:
        return ["--label", "com.sandbox-mesh.managed=true", "--label", f"com.sandbox-mesh.role={role}", "--label", f"com.sandbox-mesh.job={record['id']}"]

    def _connect_buildkit(self, network: str) -> None:
        container = self.build_config["buildkit_container"]
        self._command([*self._docker, "network", "connect", network, container], timeout=30)

    def _disconnect_buildkit(self, network: str) -> None:
        container = self.build_config["buildkit_container"]
        result = subprocess.run([*self._docker, "network", "disconnect", "--force", network, container], env=self._env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20, check=False, close_fds=True)
        detail = (result.stderr or result.stdout).lower()
        if result.returncode and not any(value in detail for value in ("not connected", "no such network", "no such container", "not found")):
            raise DriverError("BuildKit could not be detached from the completed sandbox network")

    def _isolate_buildkit_networks(self) -> None:
        container = self.build_config["buildkit_container"]
        host = json.loads(self._capture([*self._docker, "inspect", "--format", "{{json .HostConfig}}", container], timeout=15))
        expected = (int(self.build_config["buildkit_memory_bytes"]), 100000, int(round(float(self.build_config["buildkit_cpu_cores"]) * 100000)), int(self.build_config["buildkit_pids_limit"]))
        actual = (int(host.get("Memory", 0)), int(host.get("CpuPeriod", 0)), int(host.get("CpuQuota", 0)), int(host.get("PidsLimit", 0)))
        if actual != expected:
            self._command([*self._docker, "update", "--memory", str(expected[0]), "--memory-swap", str(expected[0]), "--cpu-period", str(expected[1]), "--cpu-quota", str(expected[2]), "--pids-limit", str(expected[3]), container], timeout=20)
            host = json.loads(self._capture([*self._docker, "inspect", "--format", "{{json .HostConfig}}", container], timeout=15))
            actual = (int(host.get("Memory", 0)), int(host.get("CpuPeriod", 0)), int(host.get("CpuQuota", 0)), int(host.get("PidsLimit", 0)))
            if actual != expected:
                raise DriverError("BuildKit CPU, memory, and PID limits could not be enforced")
        networks = json.loads(self._capture([*self._docker, "inspect", "--format", "{{json .NetworkSettings.Networks}}", container], timeout=15)) or {}
        for name in list(networks):
            if name == "bridge" or name.startswith("sm-net-"):
                self._disconnect_buildkit(name)
            else:
                raise DriverError("BuildKit is attached to an unexpected network; refusing repository execution")
        remaining = json.loads(self._capture([*self._docker, "inspect", "--format", "{{json .NetworkSettings.Networks}}", container], timeout=15)) or {}
        if remaining:
            raise DriverError("BuildKit must be disconnected before a job begins")

    def prepare_network(self, record: dict, plan, on_output: Callable[[str], None]) -> tuple[str, str, int]:
        identifier = record["id"]
        internal = f"sm-net-{identifier}"
        outbound = f"sm-out-{identifier}"
        proxy = f"sm-edge-{identifier}"
        host_port = self.allocate_host_port()
        created_networks = []
        try:
            for network, internal_only in ((internal, True), (outbound, False)):
                argv = [*self._docker, "network", "create", "--driver", "bridge"]
                if internal_only:
                    argv.extend(["--internal", "--opt", "com.docker.network.bridge.gateway_mode_ipv4=isolated"])
                argv.extend(["--label", "com.sandbox-mesh.managed=true", "--label", f"com.sandbox-mesh.job={identifier}", network])
                self._command(argv, timeout=30, on_output=on_output)
                created_networks.append(network)
            self._command([
                *self._docker, "run", "--detach", "--name", proxy, "--network", outbound,
                "--publish", f"127.0.0.1:{host_port}:{self.egress['ingress_port']}/tcp",
                "--user", "65534:65534", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=8m",
                "--pids-limit", "16", "--memory", "64m", "--memory-swap", "64m", "--cpus", "0.1",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
                "--env", "SANDBOX_MESH_EGRESS_ALLOWLIST=" + ",".join(self.egress["allowlist"]),
                "--env", f"SANDBOX_MESH_UPSTREAM_HOST=sm-app-{identifier}",
                "--env", f"SANDBOX_MESH_UPSTREAM_PORT={plan.port}",
                "--env", f"SANDBOX_MESH_INGRESS_PORT={self.egress['ingress_port']}",
                "--label", "com.sandbox-mesh.managed=true", "--label", f"com.sandbox-mesh.job={identifier}",
                "--label", "com.sandbox-mesh.role=edge", self.egress["proxy_image"],
            ], timeout=40, on_output=on_output)
            self._command([*self._docker, "network", "connect", "--alias", "mesh-egress", "--alias", "mesh-ingress", internal, proxy], timeout=30, on_output=on_output)
            running = self._capture([*self._docker, "inspect", "--format", "{{.State.Running}}", proxy], timeout=10)
            if running.lower() != "true":
                raise DriverError("sandbox edge proxy failed to start")
            self._wait_proxy_ready(proxy, host_port)
            self._isolate_buildkit_networks()
            self._connect_buildkit(internal)
        except Exception as exc:
            cleanup_failed = False
            for cleanup in (lambda: self._remove_container(proxy), lambda: self._remove_network(internal), lambda: self._remove_network(outbound)):
                try:
                    cleanup()
                except Exception:
                    cleanup_failed = True
            if cleanup_failed:
                raise DriverError("sandbox network setup failed and an owned resource could not be removed") from exc
            raise
        return internal, proxy, host_port

    def _wait_proxy_ready(self, proxy: str, host_port: int, timeout: int = 120) -> None:
        # A bare TCP connect to a published port proves nothing: Docker's
        # userland proxy accepts the connection even when no process
        # listens inside the container. Read an HTTP response instead:
        # the ingress proxy answers every request (502 while the sandbox
        # app is not up yet), which proves the bundle process is really
        # serving. The bundle binds its egress (8765) listener before its
        # ingress (8081) listener in the same expression, so ingress
        # serving implies egress is bound too. Without this wait the
        # first build step (BuildKit pulling through the egress proxy)
        # dials a port nothing listens on yet, because the Python bundle
        # needs seconds to start under its 0.1-CPU cap on a 1-OCPU host.
        # Measured on foundry: the bundle binds at ~20 s under load, and
        # a request round-trip through the throttled proxy can take
        # several seconds, so the per-attempt timeout must be generous
        # (a 2 s timeout never completed even while the proxy was
        # serving and logging 502s).
        import http.client
        deadline = time.monotonic() + timeout
        while True:
            try:
                connection = http.client.HTTPConnection("127.0.0.1", host_port, timeout=10)
                connection.request("GET", "/")
                response = connection.getresponse()
                response.read()
                connection.close()
                return
            except Exception:
                if time.monotonic() >= deadline:
                    raise DriverError(f"edge proxy {proxy} did not start serving on its ingress port before the readiness deadline")
                time.sleep(2)

    def release_build_network(self, network: str) -> None:
        self._disconnect_buildkit(network)

    def _buildx_args(self, record: dict, image: str, context: pathlib.Path, dockerfile: pathlib.Path, network: str, timeout: int) -> list[str]:
        if network != f"sm-net-{record['id']}":
            raise DriverError("BuildKit must join only the sandbox-owned internal network")
        build = self.build_config
        cpu_quota = int(round(float(build["buildkit_cpu_cores"]) * 100_000))
        argv = [
            *self._buildx_cli, "build", "--builder", build["buildkit_builder"],
            "--platform", self.sandbox["platform"], "--network", "host", "--allow", "network.host",
            "--resource", f"memory={build['buildkit_memory_bytes']}",
            "--resource", f"cpu-quota={cpu_quota}", "--resource", "cpu-period=100000",
            "--ulimit", f"nproc={build['buildkit_pids_limit']}:{build['buildkit_pids_limit']}",
            "--ulimit", "nofile=1024:1024", "--progress", "plain", "--load", "--provenance=false",
            "--label", "com.sandbox-mesh.managed=true", "--label", f"com.sandbox-mesh.job={record['id']}",
            "--label", f"com.sandbox-mesh.commit={record['commit_sha']}", "--tag", image,
        ]
        if self.egress["allowlist"]:
            network_name = f"sm-net-{record['id']}"
            proxy = "http://mesh-egress:8765"
            template = '{{with index .NetworkSettings.Networks "' + network_name + '"}}{{.IPAddress}}{{end}}'
            proxy_ip = self._capture([*self._docker, "inspect", "--format", template, f"sm-edge-{record['id']}"], timeout=15)
            try:
                parsed_proxy_ip = ipaddress.ip_address(proxy_ip)
            except ValueError as exc:
                raise DriverError("egress proxy has no valid per-job internal IP") from exc
            if not parsed_proxy_ip.is_private or parsed_proxy_ip.is_loopback or parsed_proxy_ip.is_unspecified:
                raise DriverError("egress proxy IP is not private to the per-job network")
            argv.extend(["--add-host", f"mesh-egress:{proxy_ip}"])
            for variable in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                argv.extend(["--build-arg", f"{variable}={proxy}"])
            no_proxy = "localhost,127.0.0.1,::1,mesh-egress"
            argv.extend(["--build-arg", f"NO_PROXY={no_proxy}", "--build-arg", f"no_proxy={no_proxy}"])
        argv.extend(["--file", str(dockerfile), str(context)])
        return argv

    def _run_buildx(self, record: dict, image: str, context: pathlib.Path, dockerfile: pathlib.Path, network: str, on_output: Callable[[str], None]) -> None:
        argv = self._buildx_args(record, image, context, dockerfile, network, self.sandbox["build_timeout_seconds"])
        self._command(argv, timeout=self.sandbox["build_timeout_seconds"], on_output=on_output, cwd=context)

    def _build_dockerfile(self, record: dict, repo_path: pathlib.Path, plan, image: str, network: str, on_output: Callable[[str], None]) -> None:
        context = plan.context.resolve(strict=True)
        dockerfile = plan.dockerfile.resolve(strict=True)
        try:
            context.relative_to(repo_path.resolve(strict=True))
            dockerfile.relative_to(repo_path.resolve(strict=True))
        except ValueError as exc:
            raise DriverError("Docker build context or Dockerfile escaped the cloned repository") from exc
        self._run_buildx(record, image, context, dockerfile, network, on_output)

    def _build_dockerfile_with_commands(self, record: dict, repo_path: pathlib.Path, plan, image: str, network: str, on_output: Callable[[str], None]) -> None:
        context = plan.context.resolve(strict=True)
        dockerfile = plan.dockerfile.resolve(strict=True)
        try:
            context.relative_to(repo_path.resolve(strict=True))
            dockerfile.relative_to(repo_path.resolve(strict=True))
        except ValueError as exc:
            raise DriverError("Docker build context or Dockerfile escaped the cloned repository") from exc
        temporary = pathlib.Path(tempfile.mkdtemp(prefix=f"dockerfile-{record['id']}-", dir=self.root / "tmp"))
        generated = temporary / "Dockerfile"
        try:
            content = dockerfile.read_text(encoding="utf-8")
            commands = "\n".join("RUN " + json.dumps(command, ensure_ascii=False) for command in plan.build_commands)
            generated.write_text(content.rstrip("\r\n") + "\n" + commands + "\n", encoding="utf-8")
            from .detector import _verify_dockerfile
            _verify_dockerfile(generated)
            self._run_buildx(record, image, context, generated, network, on_output)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def _build_post_commands(self, record: dict, base_image: str, image: str, commands: list[list[str]], network: str, on_output: Callable[[str], None]) -> None:
        temporary = pathlib.Path(tempfile.mkdtemp(prefix=f"post-{record['id']}-", dir=self.root / "tmp"))
        try:
            lines = [f"FROM {base_image}"]
            lines.extend("RUN " + json.dumps(command, ensure_ascii=False) for command in commands)
            dockerfile = temporary / "Dockerfile"
            dockerfile.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self._run_buildx(record, image, temporary, dockerfile, network, on_output)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def _build_nixpacks(self, record: dict, repo_path: pathlib.Path, plan, image: str, network: str, on_output: Callable[[str], None]) -> None:
        temporary = pathlib.Path(tempfile.mkdtemp(prefix=f"nixpacks-{record['id']}-", dir=self.root / "tmp"))
        output = temporary / "out"
        argv = [str(self._nixpacks), "build", str(repo_path), "--out", str(output), "--platform", self.sandbox["platform"]]
        if plan.build_commands:
            argv.extend(["--build-cmd", " && ".join(shlex.join(command) for command in plan.build_commands)])
        if plan.run:
            argv.extend(["--start-cmd", shlex.join(plan.run)])
        try:
            self._command(argv, timeout=60, on_output=on_output, cwd=repo_path)
            candidates = (output / ".nixpacks" / "Dockerfile", output / "Dockerfile")
            dockerfile = next((candidate for candidate in candidates if candidate.is_file()), None)
            if dockerfile is None:
                raise DriverError("Nixpacks did not produce a build plan Dockerfile")
            from .detector import _verify_dockerfile
            _verify_dockerfile(dockerfile)
            # The generated Dockerfile COPYs both the generated .nixpacks files
            # and the repository (COPY . /app/.). Neither the nixpacks --out dir
            # (no repo files) nor the clone (no generated files, and shared
            # across jobs through the clone cache) is a valid build context,
            # so stage a per-job context: the repo without .git, overlaid with
            # the generated .nixpacks files. The clone itself is never modified.
            staging = temporary / "context"
            shutil.copytree(repo_path, staging, ignore=shutil.ignore_patterns(".git"))
            generated = output / ".nixpacks"
            if generated.is_dir():
                shutil.copytree(generated, staging / ".nixpacks", dirs_exist_ok=True)
            self._run_buildx(record, image, staging, dockerfile, network, on_output)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def build(self, record: dict, repo_path: pathlib.Path, plan, network_name: str, on_output: Callable[[str], None]) -> str:
        image = self._image_tag(record)
        cached_architecture = self._image_architecture(image)
        if cached_architecture == "arm64":
            self._check_image_size(image)
            self.trim_image_cache(image)
            on_output("Using cached ARM64 image for this commit")
            return image
        if cached_architecture:
            self._remove_image(image)
        stage = None
        try:
            if plan.kind == "image":
                self._build_post_commands(record, plan.image, image, plan.build_commands, network_name, on_output)
            elif plan.kind == "buildpack":
                self._build_nixpacks(record, repo_path, plan, image, network_name, on_output)
            elif plan.kind in {"dockerfile", "devcontainer", "compose"}:
                stage = image
                if plan.build_commands:
                    self._build_dockerfile_with_commands(record, repo_path, plan, image, network_name, on_output)
                else:
                    self._build_dockerfile(record, repo_path, plan, image, network_name, on_output)
            else:
                raise DriverError("no supported sandbox build driver exists for this repository")
            if self._image_architecture(image) != "arm64":
                raise DriverError("built image is not ARM64; refusing to run it")
            self._check_image_size(image)
            self.trim_image_cache(image)
            return image
        except Exception:
            for candidate in (image, stage):
                if candidate:
                    try:
                        self._remove_image(candidate)
                    except DriverError:
                        pass
            raise

    def build_wrapper(self, record: dict, app_image: str, on_output: Callable[[str], None]) -> str:
        wrapper = self._supervisor_tag(record)
        cached_architecture = self._image_architecture(wrapper)
        if cached_architecture == "arm64":
            self._check_image_size(wrapper)
            self.trim_supervisor_cache(wrapper)
            on_output("Using cached ARM64 supervisor image for this commit")
            return wrapper
        if cached_architecture:
            self._remove_image(wrapper)
        binary = self.root / "mesh" / "supervisor" / "bin" / "sandbox-supervisor-linux-arm64"
        if not binary.is_file():
            raise DriverError("the pinned ARM64 sandbox supervisor binary is missing; run scripts/build_supervisor.py")
        with tempfile.TemporaryDirectory(prefix="sandbox-supervisor-", dir=self.root / "tmp") as temporary:
            context = pathlib.Path(temporary)
            if not os.access(binary, os.X_OK):
                raise DriverError("the ARM64 sandbox supervisor is not executable")
            header = binary.read_bytes()[:20]
            if len(header) < 20 or header[:4] != b"\x7fELF" or header[4:6] != b"\x02\x01" or int.from_bytes(header[18:20], "little") != 183:
                raise DriverError("the sandbox supervisor binary is not a verified ARM64 Linux ELF")
            shutil.copyfile(binary, context / "sandbox-supervisor")
            (context / "sandbox-supervisor").chmod(0o755)
            (context / "Dockerfile").write_text(
                'FROM app\nCOPY sandbox-supervisor /usr/local/bin/sandbox-supervisor\nENTRYPOINT ["/usr/local/bin/sandbox-supervisor"]\n',
                encoding="utf-8",
            )
            command = [
                *self._buildx_cli, "build", "--builder", self.build_config["buildkit_builder"],
                "--platform", self.sandbox["platform"], "--network", "none",
                "--resource", f"memory={self.build_config['buildkit_memory_bytes']}",
                "--resource", f"cpu-quota={int(round(float(self.build_config['buildkit_cpu_cores']) * 100_000))}",
                "--resource", "cpu-period=100000",
                "--ulimit", f"nproc={self.build_config['buildkit_pids_limit']}:{self.build_config['buildkit_pids_limit']}",
                "--ulimit", "nofile=1024:1024", "--provenance=false",
                "--build-context", f"app=docker-image://{app_image}",
                "--label", "com.sandbox-mesh.managed=true",
                "--label", "com.sandbox-mesh.role=supervisor",
                "--label", f"com.sandbox-mesh.commit={record['commit_sha']}",
                "--tag", wrapper, "--load", "--file", str(context / "Dockerfile"), str(context),
            ]
            try:
                self._command(command, timeout=int(self.sandbox["build_timeout_seconds"]), on_output=on_output)
                if self._image_architecture(wrapper) != "arm64":
                    raise DriverError("the sandbox supervisor image is not ARM64")
                self._check_image_size(wrapper)
                self.trim_supervisor_cache(wrapper)
                return wrapper
            except Exception:
                try:
                    self._remove_image(wrapper)
                except DriverError:
                    pass
                raise

    def trim_supervisor_cache(self, keep_image: str | None = None) -> None:
        image_ids = set(self._capture([*self._docker, "image", "ls", "-q", "--no-trunc", "--filter", "label=com.sandbox-mesh.role=supervisor"], timeout=30).splitlines())
        entries = []
        for identifier in image_ids:
            metadata = self._image_metadata(identifier)
            tags = metadata.get("RepoTags") or []
            if any(tag.startswith("sandbox-mesh/supervisor:") for tag in tags):
                entries.append((str(metadata.get("Created", "")), identifier))
        entries.sort(key=lambda item: item[0], reverse=True)
        active_ids = set()
        containers = self._capture([*self._docker, "ps", "-q", "--filter", "label=com.sandbox-mesh.role=app"], timeout=20).splitlines()
        for container in containers:
            try:
                active_ids.add(self._capture([*self._docker, "inspect", "--format", "{{.Image}}", container], timeout=10))
            except DriverError:
                continue
        keep = {identifier for _, identifier in entries[:int(self.sandbox["image_cache_entries"])]} | active_ids
        if keep_image:
            keep.add(self._image_metadata(keep_image).get("Id", ""))
        for _, identifier in entries:
            if identifier not in keep:
                self._remove_image(identifier)

    def allocate_host_port(self) -> int:
        import socket
        start, end = self.sandbox["port_range"]
        for port in range(start, end + 1):
            try:
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
        raise DriverError("all loopback sandbox ports 8900–8999 are occupied")

    def _application_command(self, plan, app_image: str) -> list[str]:
        if plan.entrypoint:
            command = list(plan.entrypoint) + list(plan.run or plan.command or [])
        elif plan.run:
            command = list(plan.run)
        elif plan.command:
            command = list(plan.command)
        else:
            raw = self._capture([*self._docker, "image", "inspect", "--format", "{{json .Config}}", app_image], timeout=20)
            try:
                image_config = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise DriverError("application image command could not be verified") from exc
            command = list(image_config.get("Entrypoint") or []) + list(image_config.get("Cmd") or [])
        if not command or not all(isinstance(item, str) and item for item in command):
            raise DriverError("application image does not define a safe command to supervise")
        return command

    def _assert_no_declared_volumes(self, image: str) -> None:
        raw = self._capture([*self._docker, "image", "inspect", "--format", "{{json .Config.Volumes}}", image], timeout=20)
        if raw in {"", "null", "<no value>", "{}"}:
            return
        try:
            volumes = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DriverError("image volume metadata could not be verified; refusing host-backed mounts") from exc
        if volumes:
            raise DriverError("images declaring Docker volumes are rejected; Phase 1 does not permit host-backed mounts")

    def _nixpacks_runtime_path(self, image: str) -> str:
        """PATH for a nixpacks-built image, run as a non-root user.

        Nixpacks puts provider binaries (a pip venv, node_modules/.bin)
        in NIXPACKS_PATH and merges it into PATH only via /root/.profile,
        which a non-root login shell never reads. Merge the image's own
        NIXPACKS_PATH into its PATH here instead. Returns "" when the image
        declares no NIXPACKS_PATH.
        """
        raw = self._capture([*self._docker, "image", "inspect", "--format", "{{json .Config.Env}}", image], timeout=20)
        try:
            env = json.loads(raw) if raw not in {"", "null"} else []
        except json.JSONDecodeError as exc:
            raise DriverError("image environment could not be verified") from exc
        values = {}
        for entry in env if isinstance(env, list) else []:
            if isinstance(entry, str) and "=" in entry:
                key, value = entry.split("=", 1)
                values[key] = value
        nixpacks_path = values.get("NIXPACKS_PATH", "")
        base_path = values.get("PATH", "")
        if not nixpacks_path:
            return ""
        if nixpacks_path.endswith(":"):
            return nixpacks_path + base_path
        if base_path:
            return nixpacks_path + ":" + base_path
        return nixpacks_path

    def start(self, record: dict, image: str, plan, network_name: str, on_output: Callable[[str], None]) -> dict:
        supervised = image.startswith("sandbox-mesh/supervisor:")
        app_image = self._image_tag(record) if supervised else image
        self._assert_no_declared_volumes(app_image)
        host_port = record.get("host_port")
        start_port, end_port = self.sandbox["port_range"]
        if not isinstance(host_port, int) or not start_port <= host_port <= end_port:
            raise DriverError("the authenticated ingress proxy has no valid loopback port")

        name = f"sm-app-{record['id']}"
        config = self.sandbox
        argv = [
            *self._docker, "create", "--name", name, "--platform", config["platform"], "--network", network_name,
            "--sysctl", "net.ipv4.ip_unprivileged_port_start=0",
            "--user", config["user"],
            "--read-only", "--tmpfs", f"/tmp:rw,nosuid,nodev,noexec,size={config['tmpfs_bytes']}",
            "--tmpfs", "/run:rw,nosuid,nodev,noexec,size=8m", "--shm-size", "16m",
            "--log-driver", "local", "--log-opt", "max-size=10m", "--log-opt", "max-file=1",
            # The local driver compresses rotated logs by default, and the
            # daemon rejects compression with max-file=1 ("compression cannot
            # be enabled when max file count is 1"). Keep the single 10 MB
            # file and turn compression off explicitly.
            "--log-opt", "compress=false",
            "--cpus", str(config["cpu_cores"]), "--memory", str(config["memory_bytes"]),
            "--memory-swap", str(config["memory_bytes"]), "--pids-limit", str(config["pids_limit"]),
            "--ulimit", "nofile=1024:1024", "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
            "--stop-timeout", "5", "--restart", "no", *self._label_args(record, "app"),
            "--env", f"PORT={plan.port}", "--env", "HOST=0.0.0.0",
        ]
        argv.extend(["--env", "SANDBOX_MESH_PID_DIR=/tmp"])
        if supervised:
            agent_token = secrets.token_urlsafe(32)
            agent_task = f"Emit local health heartbeats for sandbox {record['id']}; do not access repository or host data."
            argv.extend([
                "--env", f"SANDBOX_MESH_AGENT_TOKEN={agent_token}",
                "--env", f"SANDBOX_MESH_AGENT_TASK={agent_task}",
                "--env", f"SANDBOX_MESH_HEARTBEAT_INTERVAL={int(self.config['agent']['heartbeat_interval_seconds'])}",
            ])
        if plan.kind == "buildpack":
            runtime_path = self._nixpacks_runtime_path(app_image)
            if runtime_path:
                argv.extend(["--env", f"PATH={runtime_path}"])
        if self.egress["allowlist"]:
            proxy = "http://mesh-egress:8765"
            for variable in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                argv.extend(["--env", f"{variable}={proxy}"])
            argv.extend(["--env", "NO_PROXY=localhost,127.0.0.1,::1,mesh-egress", "--env", "no_proxy=localhost,127.0.0.1,::1,mesh-egress"])
        working_dir = plan.metadata.get("working_dir") if isinstance(plan.metadata, dict) else None
        if working_dir:
            argv.extend(["--workdir", working_dir])
        command_after_image: list[str] = []
        if supervised:
            command_after_image = self._application_command(plan, app_image)
            interval = int(self.config["agent"]["heartbeat_interval_seconds"])
            argv.extend(["--entrypoint", "/usr/local/bin/sandbox-supervisor"])
            command_after_image = ["--supervise", record["id"], str(interval), "--", *command_after_image]
        elif plan.entrypoint:
            argv.extend(["--entrypoint", plan.entrypoint[0]])
            command_after_image.extend(plan.entrypoint[1:])
            command_after_image.extend(plan.run or plan.command or [])
        elif plan.run:
            argv.extend(["--entrypoint", plan.run[0]])
            command_after_image.extend(plan.run[1:])
        elif plan.command:
            command_after_image.extend(plan.command)
        argv.append(image)
        argv.extend(command_after_image)
        created_output = self._command(argv, timeout=40, on_output=on_output).splitlines()
        created = created_output[-1].strip() if created_output else ""
        if not re.fullmatch(r"[0-9a-f]{12,64}", created):
            self._remove_container(name)
            raise DriverError("Docker did not return a valid sandbox container identifier")
        try:
            self._command([*self._docker, "start", created], timeout=30, on_output=on_output)
        except Exception:
            self._remove_container(created)
            raise
        return {"container_id": created, "container_name": name, "host_port": host_port, "image": image}

    def _remove_container(self, container: str | None) -> None:
        if not container:
            return
        try:
            result = subprocess.run([*self._docker, "rm", "--force", "--volumes", container], env=self._env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=25, check=False, close_fds=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DriverError("sandbox container cleanup did not complete") from exc
        detail = (result.stderr or result.stdout).lower()
        if result.returncode and "no such container" not in detail and "no such object" not in detail:
            raise DriverError("sandbox-owned container could not be removed")

    def _remove_network(self, network: str | None) -> None:
        if not network:
            return
        self._disconnect_buildkit(network)
        try:
            result = subprocess.run([*self._docker, "network", "rm", network], env=self._env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=25, check=False, close_fds=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DriverError("sandbox network cleanup did not complete") from exc
        detail = (result.stderr or result.stdout).lower()
        if result.returncode and "no such network" not in detail and "not found" not in detail:
            raise DriverError("sandbox-owned network could not be removed")

    def _cleanup(self, record: dict) -> None:
        failures = []
        identifier = record["id"]
        for label, operation, value in (
            ("app container", self._remove_container, record.get("container_id") or record.get("container_name")),
            ("edge container", self._remove_container, record.get("egress_id") or f"sm-edge-{identifier}"),
            ("job network", self._remove_network, record.get("network_name") or f"sm-net-{identifier}"),
            ("output network", self._remove_network, f"sm-out-{identifier}"),
        ):
            try:
                operation(value)
            except Exception as exc:
                failures.append(f"{label}: {exc}")
        if failures:
            raise DriverError("one or more sandbox-owned Docker resources could not be removed: " + "; ".join(failures))

    def kill_process(self, record: dict, role: str) -> None:
        if role not in {"app", "agent"} or record.get("status") != "running" or not record.get("container_name"):
            raise DriverError("only an active sandbox app or agent can be terminated")
        identifier = record.get("id", "")
        if not re.fullmatch(r"[0-9a-f]{12}", identifier):
            raise DriverError("sandbox identity is invalid")
        self._command([
            *self._docker, "exec", "--user", self.sandbox["user"], record["container_name"],
            "/usr/local/bin/sandbox-supervisor", f"--kill-{role}", identifier,
        ], timeout=10)

    def stop(self, record: dict) -> None:
        target = record.get("container_id") or record.get("container_name")
        if target:
            try:
                subprocess.run([*self._docker, "stop", "--time", "5", target], env=self._env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False, close_fds=True)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise DriverError("sandbox stop command failed or timed out") from exc
        self._cleanup(record)

    def destroy(self, record: dict) -> None:
        self._cleanup(record)

    def logs(self, record: dict, tail: int = 200) -> str:
        target = record.get("container_id") or record.get("container_name")
        if not target:
            return ""
        try:
            return self._capture([*self._docker, "logs", "--tail", str(max(1, min(tail, 2000))), target], timeout=20)
        except DriverError:
            return ""

    def follow_logs(self, record: dict, emit: Callable[[str], None], stopped: threading.Event) -> None:
        target = record.get("container_id") or record.get("container_name")
        if not target:
            return
        try:
            process = subprocess.Popen([*self._docker, "logs", "--follow", "--tail", "0", target], env=self._env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0, close_fds=True)
        except OSError:
            return
        buffer = bytearray()
        try:
            if process.stdout is None:
                return
            while not stopped.is_set() and process.poll() is None:
                readable, _, _ = select.select([process.stdout], [], [], 0.5)
                if not readable:
                    continue
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    break
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, rest = buffer.partition(b"\n")
                    buffer = bytearray(rest)
                    emit(self._redact(line.decode("utf-8", "replace").rstrip("\r")))
            if buffer and not stopped.is_set():
                emit(self._redact(buffer.decode("utf-8", "replace")))
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
            if process.stdout:
                process.stdout.close()

    def stats(self, record: dict) -> dict:
        target = record.get("container_id")
        if not target:
            return {}
        try:
            raw = self._capture([*self._docker, "stats", "--no-stream", "--format", "{{json .}}", target], timeout=10)
            value = json.loads(raw)
            return {"cpu": value.get("CPUPerc", ""), "memory": value.get("MemUsage", ""), "pids": value.get("PIDs", "")}
        except (DriverError, json.JSONDecodeError):
            return {}

    def proxy_target(self, record: dict) -> tuple[str, int]:
        port = record.get("host_port")
        start, end = self.sandbox["port_range"]
        if not isinstance(port, int) or not start <= port <= end:
            raise DriverError("sandbox loopback port is outside the reserved range")
        return "127.0.0.1", port

    def container_exists(self, record: dict) -> bool:
        target = record.get("container_id") or record.get("container_name")
        if not target:
            return False
        try:
            result = subprocess.run([*self._docker, "inspect", "--format", "{{.State.Running}}", target], env=self._env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10, check=False, close_fds=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DriverError("Docker container state could not be checked") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout).lower()
            if "no such object" in detail or "no such container" in detail:
                return False
            raise DriverError("Docker container state could not be checked")
        return result.stdout.strip().lower() == "true"

    def reconcile(self, active_ids: set[str]) -> list[str]:
        removed: list[str] = []
        containers = self._capture([*self._docker, "ps", "-aq", "--filter", "label=com.sandbox-mesh.managed=true"], timeout=20).splitlines()
        for identifier in containers:
            try:
                labels = json.loads(self._capture([*self._docker, "inspect", "--format", "{{json .Config.Labels}}", identifier], timeout=10))
            except (DriverError, json.JSONDecodeError):
                continue
            if labels.get("com.sandbox-mesh.role") == "warm":
                continue
            if labels.get("com.sandbox-mesh.job") not in active_ids:
                self._remove_container(identifier)
                removed.append(identifier)
        networks = self._capture([*self._docker, "network", "ls", "-q", "--filter", "label=com.sandbox-mesh.managed=true"], timeout=20).splitlines()
        for identifier in networks:
            try:
                labels = json.loads(self._capture([*self._docker, "network", "inspect", "--format", "{{json .Labels}}", identifier], timeout=10))
            except (DriverError, json.JSONDecodeError):
                continue
            if labels.get("com.sandbox-mesh.job") not in active_ids:
                self._remove_network(identifier)
                removed.append(identifier)
        return removed

    def ensure_warm_pool(self) -> None:
        name = "sandbox-mesh-warm"
        exists = subprocess.run([*self._docker, "inspect", name], env=self._env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False, close_fds=True).returncode == 0
        if exists:
            running = subprocess.run([*self._docker, "inspect", "--format", "{{.State.Running}}", name], env=self._env, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=False, close_fds=True)
            if running.returncode == 0 and running.stdout.strip().lower() == "true":
                return
            self._command([*self._docker, "start", name], timeout=20)
            return
        self._command([
            *self._docker, "run", "--detach", "--name", name, "--platform", "linux/arm64", "--network", "none",
            "--user", "65532:65532", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=8m",
            "--cpus", "0.05", "--memory", "64m", "--memory-swap", "64m", "--pids-limit", "8",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true", "--restart", "unless-stopped",
            "--label", "com.sandbox-mesh.managed=true", "--label", "com.sandbox-mesh.role=warm",
            self.config["warm_pool"]["image"], "node", "-e", "setInterval(()=>{},1000)",
        ], timeout=30)

    def snapshot_all(self) -> dict:
        result = {}
        for key, argv in {
            "containers": [*self._docker, "ps", "-aq", "--filter", "label=com.sandbox-mesh.managed=true"],
            "networks": [*self._docker, "network", "ls", "-q", "--filter", "label=com.sandbox-mesh.managed=true"],
            "volumes": [*self._docker, "volume", "ls", "-q", "--filter", "label=com.sandbox-mesh.managed=true"],
        }.items():
            result[key] = sorted(self._capture(argv, timeout=20).splitlines())
        return result

    def resource_counts(self) -> dict:
        return {key: len(values) for key, values in self.snapshot_all().items()}

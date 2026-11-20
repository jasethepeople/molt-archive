from __future__ import annotations

import json
import pathlib
import re
import shlex
from dataclasses import asdict, dataclass

from .config import ConfigError, load_sandbox_overrides, parse_yaml_subset


class DetectionError(ValueError):
    pass


@dataclass(frozen=True)
class BuildPlan:
    kind: str
    context: pathlib.Path
    dockerfile: pathlib.Path | None
    image: str | None
    port: int
    run: list[str] | None
    entrypoint: list[str] | None
    build_commands: list[list[str]]
    metadata: dict
    command: list[str] | None = None

    def to_dict(self, repo_root: pathlib.Path) -> dict:
        value = asdict(self)
        value["context"] = str(self.context.relative_to(repo_root))
        value["dockerfile"] = str(self.dockerfile.relative_to(repo_root)) if self.dockerfile else None
        return value


def _inside(root: pathlib.Path, candidate: pathlib.Path) -> pathlib.Path:
    candidate = candidate if candidate.is_absolute() else root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise DetectionError("build context or Dockerfile escapes the repository") from exc
    return resolved


def _strip_jsonc(text: str) -> str:
    output = []
    index = 0
    quoted = False
    escaped = False
    while index < len(text):
        char = text[index]
        if quoted:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
            continue
        if char == '"':
            quoted = True
            output.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < len(text) and text[index + 1] == "/":
            newline = text.find("\n", index + 2)
            if newline < 0:
                break
            output.append("\n")
            index = newline + 1
            continue
        if char == "/" and index + 1 < len(text) and text[index + 1] == "*":
            end = text.find("*/", index + 2)
            if end < 0:
                raise ValueError("unterminated JSONC block comment")
            output.extend("\n" for part in text[index:end + 2] if part == "\n")
            index = end + 2
            continue
        output.append(char)
        index += 1
    source = "".join(output)
    output = []
    index = 0
    quoted = False
    escaped = False
    while index < len(source):
        char = source[index]
        if quoted:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
            continue
        if char == '"':
            quoted = True
            output.append(char)
            index += 1
            continue
        if char == ",":
            following = index + 1
            while following < len(source) and source[following].isspace():
                following += 1
            if following < len(source) and source[following] in "]}":
                index += 1
                continue
        output.append(char)
        index += 1
    return "".join(output)


def _read_json(path: pathlib.Path) -> dict:
    if path.stat().st_size > 1_048_576:
        raise DetectionError(f"{path.name} is larger than the 1 MiB configuration limit")
    try:
        text = path.read_text(encoding="utf-8")
        value = json.loads(_strip_jsonc(text))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise DetectionError(f"invalid JSON in {path.name}") from exc
    if not isinstance(value, dict):
        raise DetectionError(f"{path.name} must contain a JSON object")
    return value


def _safe_yaml(path: pathlib.Path) -> dict:
    if path.stat().st_size > 2_097_152:
        raise DetectionError(f"{path.name} is larger than the 2 MiB configuration limit")
    try:
        value = parse_yaml_subset(path.read_text(encoding="utf-8"), path.name)
    except (OSError, UnicodeError, ConfigError) as exc:
        raise DetectionError(f"invalid or unsupported YAML in {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise DetectionError(f"{path.name} must contain a YAML mapping")
    return value


def _command(value, label: str) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            result = shlex.split(value, posix=True)
        except ValueError as exc:
            raise DetectionError(f"{label} has invalid quoting") from exc
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        result = value
    else:
        raise DetectionError(f"{label} must be a string or an argv list")
    if not result or len(result) > 64 or any(not item or "\x00" in item or len(item) > 4096 for item in result):
        raise DetectionError(f"{label} must contain 1–64 non-empty arguments")
    return list(result)


def _dockerfile_port(path: pathlib.Path) -> int | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        match = re.match(r"\s*EXPOSE\s+(\d+)(?:/tcp)?(?:\s|$)", line, re.IGNORECASE)
        if match:
            value = int(match.group(1))
            if 1 <= value <= 65535:
                return value
    return None


def _verify_dockerfile(path: pathlib.Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    folded = text.replace("\\\r\n", " ").replace("\\\n", " ")
    for line in folded.splitlines():
        stripped = line.strip()
        syntax = re.match(r"#\s*syntax\s*=\s*(\S+)", stripped, re.IGNORECASE)
        if syntax and not syntax.group(1).lower().startswith("docker/dockerfile:"):
            raise DetectionError("remote or custom Dockerfile frontends are disabled")
        if re.match(r"RUN\b", stripped, re.IGNORECASE) and re.search(r"--network(?:=|\s+)(?!none(?:\s|$))", stripped, re.IGNORECASE):
            raise DetectionError("Dockerfile RUN network overrides are disabled; build egress must remain deny-by-default")
        if re.match(r"FROM\b", stripped, re.IGNORECASE):
            platform = re.search(r"--platform(?:=|\s+)(\S+)", stripped, re.IGNORECASE)
            if platform and platform.group(1).lower() not in {"linux/arm64", "$buildplatform", "$targetplatform"}:
                raise DetectionError("Dockerfile explicitly selects a non-ARM64 base platform")
        if re.match(r"ADD\b", stripped, re.IGNORECASE):
            try:
                parts = shlex.split(stripped, posix=True)[1:]
            except ValueError as exc:
                raise DetectionError("Dockerfile ADD instruction has invalid quoting") from exc
            sources = [part for part in parts if not part.startswith("--")]
            if any(_remote_source(part) for part in sources[:-1]):
                raise DetectionError("remote ADD sources are disabled; use repository files so the host cannot fetch unreviewed URLs")


def _remote_source(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith(("http://", "https://", "git://", "ssh://", "git@"))


def _port(value, default: int = 8080) -> int:
    if isinstance(value, bool):
        raise DetectionError("application port must be an integer from 1 to 65535")
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise DetectionError("application port must be an integer from 1 to 65535")
    if not 1 <= port <= 65535:
        raise DetectionError("application port must be an integer from 1 to 65535")
    return port


def _forward_port(value, default: int = 8080) -> int:
    if isinstance(value, str) and ":" in value:
        value = value.rsplit(":", 1)[-1]
    return _port(value, default)


def _container_workdir(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 255 or "\\x00" in value:
        raise DetectionError("container working directory must be a short path")
    if any(part == ".." for part in value.replace("\\", "/").split("/")):
        raise DetectionError("container working directory may not traverse parent paths")
    return value

_IMAGE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:+-]{0,254}(?:@sha256:[0-9a-f]{64})?$")


def _image_reference(value) -> str:
    if not isinstance(value, str) or len(value) > 255 or not _IMAGE_REFERENCE.fullmatch(value):
        raise DetectionError("image reference contains unsupported characters")
    return value


def _compose_port(service: dict) -> int | None:
    ports = service.get("ports") or service.get("expose") or []
    if not isinstance(ports, list) or not ports:
        return None
    for entry in ports:
        if isinstance(entry, bool):
            raise DetectionError("compose port entries must be TCP ports")
        if isinstance(entry, int):
            return _port(entry)
        if isinstance(entry, str):
            if entry.rsplit("/", 1)[-1].lower() == "udp":
                raise DetectionError("UDP-only services are not supported by the HTTP reverse proxy")
            value = entry.split(":")[-1].split("/")[0]
            if value.isdigit():
                return _port(value)
        if isinstance(entry, dict):
            if str(entry.get("protocol", "tcp")).lower() != "tcp":
                raise DetectionError("UDP-only services are not supported by the HTTP reverse proxy")
            if entry.get("target"):
                return _port(entry["target"])
    return None


def _reject_compose_features(service: dict) -> None:
    allowed = {"build", "image", "command", "entrypoint", "ports", "expose", "working_dir"}
    unsupported = sorted(set(service) - allowed)
    if unsupported:
        raise DetectionError("compose service contains unsupported fields that Phase 1 will not ignore: " + ", ".join(unsupported))


def detect(repo_root: pathlib.Path) -> BuildPlan:
    repo_root = repo_root.resolve(strict=True)
    try:
        overrides = load_sandbox_overrides(repo_root)
    except ConfigError as exc:
        raise DetectionError(str(exc)) from exc
    override_port = overrides.get("port")
    override_run = overrides.get("run")
    build_commands = overrides.get("build", [])

    dockerfile = repo_root / "Dockerfile"
    if dockerfile.exists():
        dockerfile = _inside(repo_root, dockerfile)
        if not dockerfile.is_file():
            raise DetectionError("Dockerfile must be a regular file")
        _verify_dockerfile(dockerfile)
        port = _port(override_port or _dockerfile_port(dockerfile) or 8080)
        return BuildPlan("dockerfile", repo_root, dockerfile, None, port, override_run, None, build_commands, {})

    dev_files = (repo_root / ".devcontainer" / "devcontainer.json", repo_root / "devcontainer.json")
    dev_path = next((path for path in dev_files if path.is_file()), None)
    if dev_path:
        dev_path = _inside(repo_root, dev_path)
        dev = _read_json(dev_path)
        for field in ("features", "mounts", "runArgs", "containerEnv", "remoteEnv", "postCreateCommand", "postStartCommand"):
            if dev.get(field):
                raise DetectionError(f"devcontainer {field} is not applied because Phase 1 does not allow repo-defined mounts or host-side commands")
        build = dev.get("build") or {}
        if not isinstance(build, dict):
            raise DetectionError("devcontainer build must be a JSON object")
        if build.get("args"):
            raise DetectionError("devcontainer build arguments are not imported")
        context = _inside(repo_root, dev_path.parent / build.get("context", "."))
        dockerfile_value = build.get("dockerfile", "Dockerfile")
        image = dev.get("image")
        candidate = _inside(context, context / dockerfile_value) if build else None
        if candidate and candidate.is_file():
            _verify_dockerfile(candidate)
            ports = dev.get("forwardPorts", dev.get("appPort", []))
            if isinstance(ports, (int, str)):
                ports = [ports]
            port = override_port or (_forward_port(ports[0]) if ports else _dockerfile_port(candidate) or 8080)
            metadata = {"working_dir": _container_workdir(dev.get("workspaceFolder"))}
            return BuildPlan("devcontainer", context, candidate, None, _port(port), override_run, None, build_commands, metadata)
        if build:
            raise DetectionError("devcontainer build did not resolve to a Dockerfile inside the repository")
        if isinstance(image, str) and image:
            ports = dev.get("forwardPorts", dev.get("appPort", []))
            if isinstance(ports, (int, str)):
                ports = [ports]
            port = override_port or (_forward_port(ports[0]) if ports else 8080)
            metadata = {"source": "devcontainer", "working_dir": _container_workdir(dev.get("workspaceFolder"))}
            return BuildPlan("image", context, None, _image_reference(image), _port(port), override_run, None, build_commands, metadata)
        raise DetectionError("devcontainer.json must define a Dockerfile build or a published image")

    compose_names = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
    compose_path = next((repo_root / name for name in compose_names if (repo_root / name).exists()), None)
    if compose_path:
        compose_path = _inside(repo_root, compose_path)
        compose = _safe_yaml(compose_path)
        unsupported_root = sorted(set(compose) - {"version", "name", "services"})
        if unsupported_root:
            raise DetectionError("compose file contains unsupported top-level fields: " + ", ".join(unsupported_root))
        services = compose.get("services")
        if not isinstance(services, dict) or len(services) != 1:
            raise DetectionError("Phase 1 supports exactly one compose service; multi-service stacks are not started")
        name, service = next(iter(services.items()))
        if not isinstance(service, dict):
            raise DetectionError("compose service definition must be a mapping")
        _reject_compose_features(service)
        build = service.get("build")
        if isinstance(build, str):
            context = _inside(repo_root, repo_root / build)
            candidate = _inside(context, context / "Dockerfile")
        elif isinstance(build, dict):
            if any(build.get(key) for key in ("args", "additional_contexts", "dockerfile_inline", "secrets", "ssh", "network", "extra_hosts")):
                raise DetectionError("compose build args, extra contexts, secrets, SSH, and custom networks are disabled")
            context = _inside(repo_root, repo_root / build.get("context", "."))
            candidate = _inside(context, context / build.get("dockerfile", "Dockerfile"))
        else:
            context = repo_root
            candidate = None
        image = service.get("image")
        if build is not None and (candidate is None or not candidate.is_file()):
            raise DetectionError("compose build did not resolve to a Dockerfile inside the repository")
        if candidate and candidate.is_file():
            _verify_dockerfile(candidate)
            port = override_port or _dockerfile_port(candidate) or _compose_port(service) or 8080
            run = override_run
            command = _command(service.get("command"), "compose command")
            entrypoint = _command(service.get("entrypoint"), "compose entrypoint")
            working_dir = _container_workdir(service.get("working_dir"))
            return BuildPlan("compose", context, candidate, None, _port(port), run, entrypoint, build_commands, {"service": str(name), "working_dir": working_dir}, command)
        if isinstance(image, str) and image:
            if "$" in image:
                raise DetectionError("compose image interpolation is disabled; use an explicit ARM64 image reference")
            run = override_run
            command = _command(service.get("command"), "compose command")
            entrypoint = _command(service.get("entrypoint"), "compose entrypoint")
            working_dir = _container_workdir(service.get("working_dir"))
            return BuildPlan("image", context, None, _image_reference(image), _port(override_port or _compose_port(service) or 8080), run, entrypoint, build_commands, {"service": str(name), "working_dir": working_dir}, command)
        raise DetectionError("the single compose service must define a Dockerfile build or an explicit image")

    markers = (
        ("package.json", "node", 3000),
        ("pyproject.toml", "python", 8000),
        ("requirements.txt", "python", 8000),
        ("go.mod", "go", 8080),
        ("Cargo.toml", "rust", 8080),
    )
    for marker, language, default_port in markers:
        if (repo_root / marker).is_file():
            return BuildPlan("buildpack", repo_root, None, None, _port(override_port or default_port), override_run, None, build_commands, {"language": language})
    raise DetectionError("no Dockerfile, devcontainer, compose file, or supported Node/Python/Go/Rust build marker was found")

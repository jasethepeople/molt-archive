from __future__ import annotations

import base64
import os
import pathlib
import re
import shutil
import subprocess
import urllib.parse


class RepositoryError(ValueError):
    pass


GIT_SHA = re.compile(r"^[0-9a-f]{40,64}$")


def canonical_source(url: str, project_root: pathlib.Path) -> tuple[str, bool]:
    if not isinstance(url, str) or len(url) > 2048:
        raise RepositoryError("repository URL is missing or too long")
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme == "https" and parsed.hostname and parsed.hostname.lower() == "github.com":
        if parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
            raise RepositoryError("GitHub URLs must not contain credentials, ports, queries, or fragments")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2 or any(part in {".", ".."} for part in parts):
            raise RepositoryError("enter a GitHub repository URL such as https://github.com/owner/repo")
        owner, repository = parts
        if repository.endswith(".git"):
            repository = repository[:-4]
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository):
            raise RepositoryError("GitHub owner or repository name contains unsupported characters")
        return f"https://github.com/{owner}/{repository}.git", False
    if parsed.scheme == "file" and parsed.netloc in {"", "localhost"} and not parsed.query and not parsed.fragment:
        source = pathlib.Path(urllib.parse.unquote(parsed.path)).resolve()
        samples = (project_root / "samples").resolve()
        if not source.is_relative_to(samples) or not (source / ".git").exists():
            raise RepositoryError("file URLs are restricted to Git repositories under this project's samples/ directory")
        return source.as_uri(), True
    raise RepositoryError("only public/private GitHub HTTPS URLs are accepted; local file URLs are limited to bundled demo samples")


def _git_env(token: str | None, project_root: pathlib.Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_TRACE": "0",
        "GIT_TRACE_CURL": "0",
        "HOME": str(project_root / "data" / "git-home"),
    })
    if token:
        auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {auth}",
        })
    else:
        env["GIT_CONFIG_COUNT"] = "0"
    return env


def _run_git(args: list[str], env: dict[str, str], token: str | None, timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False, env=env)
    except subprocess.TimeoutExpired as exc:
        raise RepositoryError("git operation timed out") from exc
    except OSError as exc:
        raise RepositoryError(f"git could not start: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "git failed").strip()
        if token:
            detail = detail.replace(token, "[redacted]")
        detail = re.sub(r"https://[^\s@]+:[^\s@]+@", "https://[redacted]@", detail)
        raise RepositoryError(detail[-2000:])
    return result


def _tree_size(path: pathlib.Path, maximum: int) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_symlink():
            raise RepositoryError("repository symlinks are rejected so build planning cannot read outside the checkout")
        if not item.is_file():
            continue
        try:
            total += item.stat(follow_symlinks=False).st_size
        except OSError:
            continue
        if total > maximum:
            raise RepositoryError(f"repository exceeds the configured {maximum // (1024 * 1024)} MiB checkout limit")
    return total


def clone_repository(url: str, project_root: pathlib.Path, cache_root: pathlib.Path, token: str | None, max_bytes: int, timeout: int = 180) -> tuple[pathlib.Path, str]:
    source, local = canonical_source(url, project_root)
    env = _git_env(token if not local else None, project_root)
    remote_sha = _run_git(["git", "ls-remote", source, "HEAD"], env, token if not local else None, min(timeout, 45)).stdout.strip().split("\t", 1)[0]
    if not GIT_SHA.fullmatch(remote_sha):
        raise RepositoryError("GitHub did not return a valid commit SHA for HEAD")
    destination = cache_root / "repos" / remote_sha
    if destination.is_dir():
        try:
            current = _run_git(["git", "-C", str(destination), "rev-parse", "HEAD"], env, token, 10).stdout.strip()
            if current == remote_sha:
                _tree_size(destination, max_bytes)
                return destination, remote_sha
        except RepositoryError:
            shutil.rmtree(destination, ignore_errors=True)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.parent / f".{remote_sha}.{os.getpid()}.clone"
    shutil.rmtree(temporary, ignore_errors=True)
    args = ["git", "clone", "--depth", "1", "--single-branch", "--no-tags", "--no-recurse-submodules", "--filter=blob:limit=50m", "-c", "core.hooksPath=/dev/null"]
    if local:
        args.extend(["-c", "protocol.file.allow=always"])
    args.extend([source, str(temporary)])
    try:
        _run_git(args, env, token if not local else None, timeout)
        cloned_sha = _run_git(["git", "-C", str(temporary), "rev-parse", "HEAD"], env, token, 15).stdout.strip()
        if cloned_sha != remote_sha:
            raise RepositoryError("cloned commit changed while the repository was being fetched")
        _tree_size(temporary, max_bytes)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination, remote_sha

from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import stat
import uuid

from .repository import RepositoryError, canonical_source, clone_repository


class GitSource:
    fixture_names = ("node-basic", "python-basic", "dockerfile-basic", "build-failure")

    def __init__(self, project_root: pathlib.Path, config: dict):
        self.root = project_root.resolve()
        self.config = config

    def _prune_cache(self, keep_commit: str) -> None:
        cache_root = self.root / "cache" / "repos"
        if not cache_root.is_dir():
            return
        entries = []
        for path in cache_root.iterdir():
            if path.is_symlink():
                raise RepositoryError("repository cache contains a symlink")
            if path.is_dir() and len(path.name) in {40, 64} and all(char in "0123456789abcdef" for char in path.name):
                try:
                    entries.append((path.stat().st_mtime, path))
                except OSError as exc:
                    raise RepositoryError("repository cache metadata could not be checked") from exc
        entries.sort(key=lambda item: item[0], reverse=True)
        keep_names = {keep_commit, *(path.name for _, path in entries[: int(self.config["sandbox"]["clone_cache_entries"])])}
        for _, path in entries:
            if path.name not in keep_names:
                try:
                    shutil.rmtree(path)
                except OSError as exc:
                    raise RepositoryError("old repository cache entry could not be removed") from exc

    def normalize(self, url: str) -> str:
        try:
            normalized, _ = canonical_source(url, self.root)
            return normalized
        except (RepositoryError, OSError, ValueError) as exc:
            raise RepositoryError(str(exc)) from exc

    def prepare(self, url: str) -> tuple[pathlib.Path, str, str]:
        normalized = self.normalize(url)
        token = os.environ.get("GITHUB_TOKEN")
        try:
            path, commit = clone_repository(
                normalized,
                self.root,
                self.root / "cache",
                token,
                int(self.config["sandbox"]["max_checkout_bytes"]),
                int(self.config["sandbox"]["clone_timeout_seconds"]),
            )
        except RepositoryError:
            raise
        except (OSError, ValueError) as exc:
            raise RepositoryError("repository clone failed safely") from exc
        try:
            os.utime(path, None)
        except OSError:
            pass
        self._prune_cache(commit)
        return path, commit, normalized

    def prepare_demo(self, name: str) -> tuple[pathlib.Path, str, str]:
        if not self.config.get("demo", {}).get("fixtures_enabled") or name not in self.fixture_names:
            raise RepositoryError("unknown bundled acceptance fixture")
        source = self.root / "samples" / name
        if not source.is_dir():
            raise RepositoryError("bundled acceptance fixture is missing")
        samples_root = (self.root / "samples").resolve(strict=True)
        root = source.resolve(strict=True)
        try:
            root.relative_to(samples_root)
        except ValueError as exc:
            raise RepositoryError("acceptance fixtures must remain under samples/") from exc
        digest = hashlib.sha256()
        total = 0
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise RepositoryError("acceptance fixtures may not contain symlinks")
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix().encode("utf-8")
            data = path.read_bytes()
            total += len(data)
            if total > int(self.config["sandbox"]["max_checkout_bytes"]):
                raise RepositoryError("acceptance fixture exceeds the configured source-size limit")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(stat.S_IMODE(path.stat().st_mode).to_bytes(4, "big"))
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        commit = digest.hexdigest()
        cache_root = self.root / "cache" / "repos"
        cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = cache_root / commit
        if destination.is_symlink():
            raise RepositoryError("acceptance cache entry is a symlink")
        if destination.is_dir():
            try:
                os.utime(destination, None)
            except OSError:
                pass
            self._prune_cache(commit)
            return destination, commit, f"demo://{name}"
        temporary = cache_root / f".demo-{uuid.uuid4().hex}"
        try:
            shutil.copytree(root, temporary, symlinks=True)
            os.replace(temporary, destination)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        self._prune_cache(commit)
        return destination, commit, f"demo://{name}"

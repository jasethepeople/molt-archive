from __future__ import annotations

import pathlib
import json
import tempfile
import unittest
from unittest import mock

from mesh.config import ConfigError, load_config, parse_yaml_subset
from mesh.detector import DetectionError, detect
from mesh.git_source import GitSource
from mesh.repository import RepositoryError, _git_env, _run_git, canonical_source, clone_repository
from mesh.tailscale import TailscaleServeError, ensure_private_serve


ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP_ROOT = ROOT / "tmp"
TMP_ROOT.mkdir(exist_ok=True)


class Phase1ConfigTests(unittest.TestCase):
    def test_foundry_phase_scope_and_resource_caps(self):
        config = load_config(ROOT)
        self.assertEqual(config["host_id"], "foundry")
        self.assertEqual(config["driver"], "docker")
        self.assertEqual(config["resources"]["concurrency_cap"], 1)
        self.assertTrue(config["features"]["phase_1"])
        self.assertTrue(config["features"]["phase_2"])
        self.assertEqual([config["features"][f"phase_{phase}"] for phase in range(3, 6)], [False] * 3)
        self.assertEqual(config["scheduler"]["host_concurrency_caps"]["foundry"], 1)
        self.assertGreaterEqual(config["runtime"]["concurrency_cap"], 1)
        self.assertLessEqual(config["runtime"]["concurrency_cap"], config["scheduler"]["max_concurrency_cap"])
        self.assertEqual(config["sandbox"]["platform"], "linux/arm64")
        self.assertEqual(config["sandbox"]["port_range"], [8900, 8999])
        self.assertTrue(config["egress"]["default_deny"])
        self.assertEqual(config["build"]["buildkit_max_parallelism"], 1)

    def test_repeated_yaml_keys_and_unsafe_yaml_features_are_rejected(self):
        with self.assertRaises(ConfigError):
            parse_yaml_subset("x: 1\nx: 2\n")
        with self.assertRaises(ConfigError):
            parse_yaml_subset("x: !!python/object/apply:os.system [true]\n")

    def test_detector_prefers_dockerfile_and_enforces_arm64_network_policy(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
            root = pathlib.Path(temporary)
            (root / "package.json").write_text('{"name":"marker"}', encoding="utf-8")
            (root / "Dockerfile").write_text("FROM python:3.12-slim\nEXPOSE 8090\n", encoding="utf-8")
            plan = detect(root)
            self.assertEqual(plan.kind, "dockerfile")
            self.assertEqual(plan.port, 8090)
            (root / "Dockerfile").write_text("FROM --platform=linux/amd64 python:3.12-slim\n", encoding="utf-8")
            with self.assertRaises(DetectionError):
                detect(root)
            (root / "Dockerfile").write_text("FROM python:3.12-slim\nRUN --network=host curl https://bad.example\n", encoding="utf-8")
            with self.assertRaises(DetectionError):
                detect(root)
            (root / "Dockerfile").write_text("FROM python:3.12-slim\nADD https://bad.example/a /tmp/a\n", encoding="utf-8")
            with self.assertRaises(DetectionError):
                detect(root)

    def test_sandbox_yaml_accepts_only_build_run_and_port(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
            root = pathlib.Path(temporary)
            (root / "package.json").write_text('{"name":"override"}', encoding="utf-8")
            (root / "sandbox.yaml").write_text('build: [["npm", "install"], ["npm", "run", "build"]]\nrun: ["node", "server.js"]\nport: 9091\n', encoding="utf-8")
            plan = detect(root)
            self.assertEqual(plan.kind, "buildpack")
            self.assertEqual(plan.port, 9091)
            self.assertEqual(plan.run, ["node", "server.js"])
            self.assertEqual(plan.build_commands, [["npm", "install"], ["npm", "run", "build"]])
            (root / "sandbox.yaml").write_text("network: host\n", encoding="utf-8")
            with self.assertRaises(DetectionError):
                detect(root)

    def test_compose_privileged_and_host_network_configurations_fail_closed(self):
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
            root = pathlib.Path(temporary)
            (root / "docker-compose.yml").write_text("services:\n  app:\n    image: example/app:latest\n    ports:\n      - \"8080:80\"\n    privileged: true\n", encoding="utf-8")
            with self.assertRaises(DetectionError):
                detect(root)
            (root / "docker-compose.yml").write_text("services:\n  app:\n    image: example/app:latest\n    ports:\n      - \"8080:80\"\n    network_mode: host\n", encoding="utf-8")
            with self.assertRaises(DetectionError):
                detect(root)

    def test_repo_url_normalization_and_file_scope(self):
        normalized, local = canonical_source("https://github.com/example/project", ROOT)
        self.assertEqual(normalized, "https://github.com/example/project.git")
        self.assertFalse(local)
        for unsafe in (
            "http://github.com/example/project",
            "https://user:secret@github.com/example/project",
            "https://github.com/example/project?token=secret",
            "https://example.com/example/project",
            "file:///etc/passwd",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(RepositoryError):
                canonical_source(unsafe, ROOT)
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
            samples = pathlib.Path(temporary) / "samples"
            repo = samples / "repo"
            (repo / ".git").mkdir(parents=True)
            source, is_local = canonical_source(repo.as_uri(), pathlib.Path(temporary))
            self.assertTrue(is_local)
            self.assertEqual(source, repo.as_uri())

    def test_git_token_is_only_in_environment_and_git_errors_are_redacted(self):
        token = "private-test-token-value"
        env = _git_env(token, ROOT)
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertNotIn(token, env["GIT_CONFIG_VALUE_0"])
        completed = mock.Mock(returncode=128, stderr=f"fatal: token {token}", stdout="")
        with mock.patch("mesh.repository.subprocess.run", return_value=completed):
            with self.assertRaises(RepositoryError) as error:
                _run_git(["git", "ls-remote"], env, token)
        self.assertNotIn(token, str(error.exception))

    def test_clone_is_shallow_content_addressed_and_reuses_the_resolved_commit(self):
        sha = "a" * 40
        calls = []
        clone_calls = []

        def run(args, **kwargs):
            calls.append(list(args))
            if "ls-remote" in args:
                return mock.Mock(returncode=0, stdout=f"{sha}\tHEAD\n", stderr="")
            if "clone" in args:
                destination = pathlib.Path(args[-1])
                destination.mkdir(parents=True)
                (destination / "README.md").write_text("fixture repository\n", encoding="utf-8")
                clone_calls.append(list(args))
                return mock.Mock(returncode=0, stdout="", stderr="")
            if "rev-parse" in args:
                return mock.Mock(returncode=0, stdout=sha + "\n", stderr="")
            raise AssertionError(f"unexpected git command: {args}")

        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
            cache = pathlib.Path(temporary) / "cache"
            with mock.patch("mesh.repository.subprocess.run", side_effect=run):
                path, first_sha = clone_repository("https://github.com/example/project", ROOT, cache, None, 1024, 30)
                cached_path, second_sha = clone_repository("https://github.com/example/project", ROOT, cache, None, 1024, 30)
            self.assertEqual(first_sha, sha)
            self.assertEqual(second_sha, sha)
            self.assertEqual(path, cached_path)
            self.assertEqual(path, cache / "repos" / sha)
        self.assertEqual(len(clone_calls), 1)
        clone = clone_calls[0]
        self.assertIn("--depth", clone)
        self.assertIn("--single-branch", clone)
        self.assertIn("--no-tags", clone)
        self.assertIn("--no-recurse-submodules", clone)
        self.assertIn("--filter=blob:limit=50m", clone)
        self.assertIn("core.hooksPath=/dev/null", clone)
        self.assertFalse(any("token" in value.lower() for call in calls for value in call))

    def test_demo_fixtures_are_content_addressed_without_git_commits(self):
        source = GitSource(ROOT, load_config(ROOT))
        path, digest, url = source.prepare_demo("node-basic")
        self.assertEqual(url, "demo://node-basic")
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertTrue(path.is_dir())
        self.assertFalse((path / ".git").exists())


class TailscaleServeTests(unittest.TestCase):
    def _result(self, value: dict, returncode: int = 0):
        return mock.Mock(returncode=returncode, stdout=json.dumps(value), stderr="")

    def test_private_route_is_added_only_when_port_is_unused(self):
        empty = {"TCP": {}, "Web": {}, "AllowFunnel": {}}
        configured = {"TCP": {}, "Web": {"foundry.example.ts.net:8787": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}}}, "AllowFunnel": {}}
        with mock.patch("mesh.tailscale.shutil.which", return_value="/usr/bin/tailscale"), mock.patch("mesh.tailscale.subprocess.run", side_effect=[self._result(empty), self._result({}), self._result(configured)]) as run:
            message = ensure_private_serve()
        self.assertIn("configured privately", message)
        self.assertEqual(run.call_args_list[1].args[0], ["/usr/bin/tailscale", "serve", "--bg", "--https=8787", "http://127.0.0.1:8787"])

    def test_existing_route_and_funnel_conflicts_are_never_overwritten(self):
        conflict = {"TCP": {}, "Web": {"foundry.example.ts.net:8787": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9000"}}}}, "AllowFunnel": {}}
        with mock.patch("mesh.tailscale.shutil.which", return_value="/usr/bin/tailscale"), mock.patch("mesh.tailscale.subprocess.run", return_value=self._result(conflict)) as run:
            with self.assertRaises(TailscaleServeError):
                ensure_private_serve()
        run.assert_called_once()
        funnel = {"TCP": {}, "Web": {}, "AllowFunnel": {"foundry.example.ts.net:8787": True}}
        with mock.patch("mesh.tailscale.shutil.which", return_value="/usr/bin/tailscale"), mock.patch("mesh.tailscale.subprocess.run", return_value=self._result(funnel)) as run:
            with self.assertRaises(TailscaleServeError):
                ensure_private_serve()
        run.assert_called_once()

    def test_existing_matching_private_route_is_left_unchanged(self):
        existing = {"TCP": {}, "Web": {"foundry.example.ts.net:8787": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}}}, "AllowFunnel": {}}
        with mock.patch("mesh.tailscale.shutil.which", return_value="/usr/bin/tailscale"), mock.patch("mesh.tailscale.subprocess.run", return_value=self._result(existing)) as run:
            message = ensure_private_serve()
        self.assertIn("already routes", message)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()

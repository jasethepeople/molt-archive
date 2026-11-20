from __future__ import annotations

import hashlib
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from mesh.config import load_config
from mesh.docker_driver import DockerDriver, DriverError


ROOT = pathlib.Path(__file__).resolve().parents[1]
RECORD = {"id": "0123456789ab", "commit_sha": "a" * 40, "host_port": 8901, "repo_path": str(ROOT)}


class SupervisorImageTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT)
        self.driver = DockerDriver(ROOT, self.config)
        (ROOT / "tmp").mkdir(parents=True, exist_ok=True)

    def test_wrapper_build_is_offline_and_pins_verified_arm64_binary(self):
        command = {}

        def capture(argv, **kwargs):
            command["argv"] = argv
            dockerfile = pathlib.Path(argv[argv.index("--file") + 1])
            command["dockerfile"] = dockerfile.read_text(encoding="utf-8")
            self.assertTrue((dockerfile.parent / "sandbox-supervisor").is_file())
            return ""

        with mock.patch.object(self.driver, "_image_architecture", side_effect=[None, "arm64"]), mock.patch.object(self.driver, "_command", side_effect=capture), mock.patch.object(self.driver, "_check_image_size"), mock.patch.object(self.driver, "trim_supervisor_cache"):
            wrapper = self.driver.build_wrapper(RECORD, "sandbox-mesh/app:test", lambda _: None)
        args = command["argv"]
        self.assertIn("--network", args)
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertIn("--build-context", args)
        self.assertIn("app=docker-image://sandbox-mesh/app:test", args)
        self.assertEqual(wrapper, args[args.index("--tag") + 1])
        self.assertIn("FROM app", command["dockerfile"])
        self.assertIn("COPY sandbox-supervisor /usr/local/bin/sandbox-supervisor", command["dockerfile"])
        self.assertIn('ENTRYPOINT ["/usr/local/bin/sandbox-supervisor"]', command["dockerfile"])

    def test_wrapper_rejects_binary_that_does_not_match_configured_hash(self):
        config = {**self.config, "agent": {**self.config["agent"], "supervisor_sha256": "0" * 64}}
        driver = DockerDriver(ROOT, config)
        with self.assertRaisesRegex(DriverError, "pinned SHA-256"):
            driver._supervisor_tag(RECORD)

    def test_container_starts_supervisor_as_pid_one_with_phase_one_limits(self):
        plan = SimpleNamespace(kind="buildpack", port=8080, entrypoint=None, run=None, command=["python", "app.py"], metadata={})
        created_id = "b" * 64
        captured = []

        def command(argv, **kwargs):
            captured.append(argv)
            return created_id + "\n" if "create" in argv else ""

        with mock.patch.object(self.driver, "_assert_no_declared_volumes") as no_volumes, mock.patch.object(self.driver, "_nixpacks_runtime_path", return_value="/usr/bin"), mock.patch.object(self.driver, "_command", side_effect=command):
            result = self.driver.start(RECORD, "sandbox-mesh/supervisor:test", plan, "sm-net-0123456789ab", lambda _: None)
        create = captured[0]
        no_volumes.assert_called_once_with(self.driver._image_tag(RECORD))
        self.assertEqual(result["container_id"], created_id)
        self.assertEqual(create[create.index("--user") + 1], self.driver.sandbox["user"])
        self.assertIn("--read-only", create)
        self.assertEqual(create[create.index("--pids-limit") + 1], str(self.driver.sandbox["pids_limit"]))
        self.assertEqual(create[create.index("--entrypoint") + 1], "/usr/local/bin/sandbox-supervisor")
        self.assertIn("--supervise", create)
        self.assertIn("0123456789ab", create)
        self.assertEqual(create[create.index("--network") + 1], "sm-net-0123456789ab")
        self.assertNotIn("--privileged", create)
        self.assertEqual(create.count("--network"), 1)
        self.assertNotIn("GITHUB_TOKEN", create)
        agent_env = [argument for argument in create if argument.startswith("SANDBOX_MESH_AGENT_")]
        self.assertEqual(len(agent_env), 2)
        token = next(argument.split("=", 1)[1] for argument in agent_env if argument.startswith("SANDBOX_MESH_AGENT_TOKEN="))
        self.assertEqual(len(token), 43)
        self.assertIn("SANDBOX_MESH_AGENT_TASK=Emit local health heartbeats for sandbox 0123456789ab; do not access repository or host data.", agent_env)

    def test_driver_can_kill_only_the_selected_supervised_process(self):
        record = {**RECORD, "status": "running", "container_name": "sm-app-0123456789ab"}
        with mock.patch.object(self.driver, "_command") as command:
            self.driver.kill_process(record, "app")
        argv = command.call_args.args[0]
        self.assertIn("exec", argv)
        self.assertIn("--kill-app", argv)
        self.assertIn("0123456789ab", argv)
        with self.assertRaisesRegex(DriverError, "only an active"):
            self.driver.kill_process(record, "host")

    def test_supervisor_build_script_and_installer_pin_match_the_artifact(self):
        binary = ROOT / "mesh/supervisor/bin/sandbox-supervisor-linux-arm64"
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        self.assertEqual(self.config["agent"]["supervisor_sha256"], digest)
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("SUPERVISOR_SHA", installer)
        self.assertIn("sudo tailscale set --operator=$USER", installer)
        self.assertIn("sha256sum -c", installer)
        script = (ROOT / "scripts/build_supervisor.py").read_text(encoding="utf-8")
        self.assertIn("supervisor_sha256", script)
        result = subprocess.run(["bash", "-n", str(ROOT / "install.sh")], check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for entrypoint in (ROOT / "install.sh", ROOT / "demo.sh"):
            self.assertNotEqual(entrypoint.stat().st_mode & 0o111, 0, f"{entrypoint.name} must be executable")


class FleetDashboardTests(unittest.TestCase):
    def test_dashboard_exposes_queue_health_agent_status_and_card_actions(self):
        html = (ROOT / "mesh/web/index.html").read_text(encoding="utf-8")
        for required in ("/api/sandboxes", "/api/scheduler", "/api/health", "queue_position", "queue_reason", "agent_status", "agent_last_heartbeat", "Queue position", "Cancel", "Restart", "Destroy", "CPU", "Memory", "PIDs", "logs/ws"):
            self.assertIn(required, html)
        self.assertIn("grid-template-columns:repeat(auto-fit", html)
        if shutil.which("node"):
            with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8") as script:
                script.write(html.split("<script>", 1)[1].split("</script>", 1)[0])
                script.flush()
                result = subprocess.run(["node", "--check", script.name], check=False, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

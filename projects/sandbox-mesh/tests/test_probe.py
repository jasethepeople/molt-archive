from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
import unittest

from mesh.config import load_config
from scripts.probe import derive_concurrency_cap


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ProbeTests(unittest.TestCase):
    def test_profile_is_written_and_execution_stays_disabled(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".test-probe-", dir=ROOT) as output:
            result = subprocess.run(
                ["bash", str(ROOT / "probe.sh"), "--host-id", "test-host", "--output-dir", output],
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            profile = json.loads((pathlib.Path(output) / "host-profile.json").read_text())
            self.assertEqual(profile["host_id"], "test-host")
            self.assertIn(profile["sandbox_driver"], {"docker", "podman-rootless", "nsjail", "bubblewrap-cgroup-v2", "userns-ulimits-private-netns", "none"})
            self.assertGreaterEqual(profile["resources"]["concurrency_cap"], 1)
            self.assertLessEqual(profile["resources"]["concurrency_cap"], profile["resources"]["max_concurrency_cap"])
            self.assertFalse(profile["repository_execution_enabled"])
            self.assertTrue((pathlib.Path(output) / "probe-report.md").is_file())

    def test_probe_cap_uses_cpu_memory_and_host_config(self) -> None:
        config = load_config(ROOT)
        scheduler = config["scheduler"]
        sandbox = config["sandbox"]
        ample_memory = int(scheduler["capacity_reserve_memory_bytes"]) + int(sandbox["memory_bytes"]) * int(scheduler["max_concurrency_cap"])
        foundry = derive_concurrency_cap(scheduler, sandbox, "foundry", 64.0, ample_memory)
        self.assertEqual(foundry, int(scheduler["host_concurrency_caps"]["foundry"]))
        cpu_limited = derive_concurrency_cap(scheduler, sandbox, "test-host", float(sandbox["cpu_cores"]) * 2, ample_memory)
        expected = min(int(scheduler["default_concurrency_cap"]), int(scheduler["max_concurrency_cap"]), 2)
        self.assertEqual(cpu_limited, expected)

    def test_output_cannot_escape_project_directory(self) -> None:
        result = subprocess.run(
            ["bash", str(ROOT / "probe.sh"), "--output-dir", str(ROOT.parent / "probe-outside")],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("must stay inside", result.stderr)

    def test_host_identifier_is_restricted(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".test-probe-", dir=ROOT) as output:
            result = subprocess.run(
                ["bash", str(ROOT / "probe.sh"), "--host-id", "../bad", "--output-dir", output],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("--host-id", result.stderr)


if __name__ == "__main__":
    unittest.main()

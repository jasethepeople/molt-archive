from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def verify_elf(path: pathlib.Path, architecture: str) -> None:
    data = path.read_bytes()[:20]
    if len(data) < 20 or data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise RuntimeError("supervisor must be a little-endian 64-bit Linux ELF")
    machine = struct.unpack_from("<H", data, 18)[0]
    expected = {"arm64": 183, "amd64": 62}[architecture]
    if machine != expected:
        raise RuntimeError(f"supervisor architecture mismatch: expected {architecture}, ELF machine={machine}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the dependency-free Sandbox Mesh process supervisor.")
    parser.add_argument("--arch", choices=("arm64", "amd64"), default="arm64")
    args = parser.parse_args()
    go = shutil.which("go")
    if not go:
        print("Go is required to build the supervisor; the release archive includes its ARM64 binary.", file=sys.stderr)
        return 2
    output = ROOT / "mesh" / "supervisor" / "bin" / f"sandbox-supervisor-linux-{args.arch}"
    output.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({"GOOS": "linux", "GOARCH": args.arch, "CGO_ENABLED": "0", "GO111MODULE": "off", "GOTOOLCHAIN": "local"})
    command = [go, "build", "-trimpath", "-ldflags=-s -w -buildid=", "-o", str(output), "./mesh/supervisor"]
    result = subprocess.run(command, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        print(result.stderr[-4000:], file=sys.stderr)
        return result.returncode
    os.chmod(output, 0o755)
    verify_elf(output, args.arch)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    if args.arch == "arm64":
        config_path = ROOT / "config.yaml"
        config_text = config_path.read_text(encoding="utf-8")
        updated, count = re.subn(r"(?m)^  supervisor_sha256: [0-9a-f]{64}$", f"  supervisor_sha256: {digest}", config_text)
        if count != 1:
            raise RuntimeError("config.yaml must contain exactly one agent.supervisor_sha256 field")
        if updated != config_text:
            config_path.write_text(updated, encoding="utf-8")
    print(f"{output.relative_to(ROOT)} sha256={digest} bytes={output.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

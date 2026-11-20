from __future__ import annotations

import argparse
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mesh.demo import Client, DemoError, benchmark


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure cached Sandbox Mesh starts on Foundry")
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.runs <= 20:
        parser.error("--runs must be between 1 and 20")
    token = os.environ.get("SANDBOX_MESH_TOKEN", "")
    if len(token) < 32:
        print("SANDBOX_MESH_TOKEN must be set in this shell", file=sys.stderr)
        return 2
    try:
        client = Client(token)
        client.login()
    except Exception as exc:
        print(f"Benchmark login failed: {type(exc).__name__}: {str(exc)[:240]}", file=sys.stderr)
        return 2
    failures: list[str] = []
    result = benchmark(client, failures, args.runs)
    if result is None or failures:
        print("Benchmark failed: " + (", ".join(failures) or "no cached start completed"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

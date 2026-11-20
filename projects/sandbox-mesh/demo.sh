#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
EXPECTED="$(realpath -m "$HOME/sandbox-mesh")"
[ "$ROOT" = "$EXPECTED" ] || { printf 'Demo must run from ~/sandbox-mesh on Foundry.\n' >&2; exit 2; }
[ -n "${SANDBOX_MESH_TOKEN:-}" ] || { printf 'Set SANDBOX_MESH_TOKEN in this shell before running the acceptance demo.\n' >&2; exit 2; }
command -v python3 >/dev/null || { printf 'Python 3 is required.\n' >&2; exit 2; }
cd "$ROOT"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
python3 -m unittest discover -s tests -p 'test_*.py' -v
exec python3 -m mesh.demo

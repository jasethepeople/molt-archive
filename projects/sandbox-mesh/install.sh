#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
HOST_PROFILE="$ROOT/host-profile.json"
[ -f "$HOST_PROFILE" ] || HOST_PROFILE="$ROOT/host-profile-foundry.json"
EXPECTED="$(realpath -m "$HOME/sandbox-mesh")"
[ "$ROOT" = "$EXPECTED" ] || { printf 'Install must run from ~/sandbox-mesh on foundry.\n' >&2; exit 2; }
cd "$ROOT"
fail(){ printf 'Install failed: %s\n' "$1" >&2; exit 1; }
[ -n "${SANDBOX_MESH_TOKEN:-}" ] || fail 'export SANDBOX_MESH_TOKEN in this shell; the installer never writes it to a file.'
python3 - <<'PY' || fail 'SANDBOX_MESH_TOKEN must contain at least 32 printable non-space characters.'
import os
v=os.environ.get('SANDBOX_MESH_TOKEN','')
raise SystemExit(0 if len(v)>=32 and all(33<=ord(c)<=126 for c in v) else 1)
PY
command -v docker >/dev/null || fail 'Docker CLI is not installed; no fallback driver is permitted.'
command -v systemctl >/dev/null || fail 'systemd is required on Foundry.'
systemctl --user show-environment >/dev/null 2>&1 || fail 'the systemd user manager is unavailable for this login.'
mkdir -p "$ROOT/data/docker-config/cli-plugins" "$ROOT/cache/repos" "$ROOT/cache/images" "$ROOT/logs" "$ROOT/tmp" "$ROOT/.tools"
chmod 700 "$ROOT/data" "$ROOT/data/docker-config" "$ROOT/data/docker-config/cli-plugins" "$ROOT/cache" "$ROOT/cache/repos" "$ROOT/cache/images" "$ROOT/logs" "$ROOT/tmp" "$ROOT/.tools"
config_tmp="$ROOT/data/docker-config/config.json.new"
printf '{"auths":{}}\n' > "$config_tmp"
chmod 600 "$config_tmp"
if ! cmp -s "$config_tmp" "$ROOT/data/docker-config/config.json" 2>/dev/null; then mv -f "$config_tmp" "$ROOT/data/docker-config/config.json"; else rm -f "$config_tmp"; fi
export DOCKER_CONFIG="$ROOT/data/docker-config"
export DOCKER_HOST="unix:///var/run/docker.sock"
docker(){ command docker --config "$DOCKER_CONFIG" --host "$DOCKER_HOST" "$@"; }
command -v python3 >/dev/null || fail 'Python 3 is not installed.'
python="$(command -v python3)"
SANDBOX_MESH_HOST_ID=foundry "$python" -m mesh.server --project-root "$ROOT" --check-config >/dev/null || fail 'config.yaml does not match the measured Foundry host settings.'
HOST_PROFILE="$HOST_PROFILE" "$python" - <<'PY' || fail 'the available Foundry host profile does not confirm Docker, ARM64, and concurrency cap 1.'
import json, os, pathlib
p=json.loads(pathlib.Path(os.environ['HOST_PROFILE']).read_text())
assert p.get('host_id') == 'foundry'
assert p.get('sandbox_driver') == 'docker'
assert p.get('os',{}).get('machine') in {'aarch64','arm64'}
assert p.get('resources',{}).get('concurrency_cap') == 1
PY
SUPERVISOR="$ROOT/mesh/supervisor/bin/sandbox-supervisor-linux-arm64"
[ -x "$SUPERVISOR" ] || fail 'the bundled ARM64 supervisor binary is missing or not executable.'
SUPERVISOR_SHA="$(sed -n 's/^  supervisor_sha256: //p' "$ROOT/config.yaml")"
[ "${#SUPERVISOR_SHA}" = 64 ] || fail 'the supervisor SHA-256 pin is missing from config.yaml.'
printf '%s  %s\n' "$SUPERVISOR_SHA" "$SUPERVISOR" | sha256sum -c - >/dev/null || fail 'the bundled supervisor binary does not match config.yaml.'
"$python" - "$SUPERVISOR" <<'PY' || fail 'the bundled supervisor is not a Linux ARM64 ELF.'
import pathlib, struct, sys
b=pathlib.Path(sys.argv[1]).read_bytes()[:20]
assert len(b)==20 and b[:6]==b'\x7fELF\x02\x01' and struct.unpack_from('<H',b,18)[0]==183
PY
info="$(docker info --format '{{.OSType}}|{{.Architecture}}|{{.CgroupVersion}}|{{.DefaultRuntime}}' 2>/dev/null)" || fail 'Docker is not responding; refusing to run repositories without the verified Docker driver.'
[ "$info" = 'linux|aarch64|2|runc' ] || [ "$info" = 'linux|arm64|2|runc' ] || fail 'Docker must report Linux ARM64, cgroups v2, and runc.'
if ! systemctl --user is-active --quiet sandbox-mesh.service; then
  "$python" - <<'PY' || fail '127.0.0.1:8787 is already occupied.'
import socket
s=socket.socket(); s.settimeout(.2)
try:
 s.connect(('127.0.0.1',8787))
except OSError:
 raise SystemExit(0)
raise SystemExit(1)
PY
fi
BUILDX_VERSION='0.39.0'
BUILDX_SHA256='722cdcdfdd6f3fb943fa1ba75102d057feb18ba72aab93b1b0a8cf2b2b48a25f'
BUILDX="$DOCKER_CONFIG/cli-plugins/docker-buildx"
if [ ! -x "$BUILDX" ] || ! "$BUILDX" version 2>/dev/null | grep -Fq "$BUILDX_VERSION"; then
  command -v curl >/dev/null || fail 'curl is required to install the pinned ARM64 Buildx CLI.'
  temp="$ROOT/.tools/buildx-v${BUILDX_VERSION}.new"
  curl -fL --retry 3 --connect-timeout 10 --max-time 240 "https://github.com/docker/buildx/releases/download/v${BUILDX_VERSION}/buildx-v${BUILDX_VERSION}.linux-arm64" -o "$temp" || fail 'could not download the ARM64 Buildx CLI.'
  printf '%s  %s\n' "$BUILDX_SHA256" "$temp" | sha256sum -c - >/dev/null || fail 'Buildx CLI checksum did not match the pinned release.'
  install -m 0755 "$temp" "$BUILDX.new"
  mv -f "$BUILDX.new" "$BUILDX"
  rm -f "$temp"
fi
buildx_version="$(docker buildx version 2>/dev/null)" || fail 'Docker CLI did not load the project-pinned Buildx plugin.'
printf '%s' "$buildx_version" | grep -Fq "$BUILDX_VERSION" || fail 'Buildx is not the pinned version.'
build_help="$(docker buildx build --help 2>/dev/null)" || fail 'Buildx build help is unavailable.'
printf '%s' "$build_help" | grep -Fq -- '--resource' || fail 'Buildx does not expose the required per-step CPU/memory resource limits.'
printf '%s' "$build_help" | grep -Fq -- '--ulimit' || fail 'Buildx does not expose process-count ulimits.'
NIXPACKS_VERSION='1.41.0'
NIXPACKS_SHA256='912bd02dd2bb6f9c3a9ed965fe8a68b4aa318dc7a2546e2eca6f2806a894ba39'
NIXPACKS="$ROOT/.tools/nixpacks"
if [ ! -x "$NIXPACKS" ] || ! "$NIXPACKS" --version 2>/dev/null | grep -Fq "$NIXPACKS_VERSION"; then
  temp="$(mktemp -d "$ROOT/.tools/nixpacks-install.XXXXXX")"
  trap 'rm -rf "$temp"' EXIT
  curl -fL --retry 3 --connect-timeout 10 --max-time 240 "https://github.com/railwayapp/nixpacks/releases/download/v${NIXPACKS_VERSION}/nixpacks-v${NIXPACKS_VERSION}-aarch64-unknown-linux-musl.tar.gz" -o "$temp/nixpacks.tar.gz" || fail 'could not download the pinned ARM64 Nixpacks binary.'
  printf '%s  %s\n' "$NIXPACKS_SHA256" "$temp/nixpacks.tar.gz" | sha256sum -c - >/dev/null || fail 'Nixpacks checksum did not match the pinned release.'
  mkdir "$temp/extracted"
  tar -xzf "$temp/nixpacks.tar.gz" -C "$temp/extracted"
  source_binary="$(find "$temp/extracted" -type f -name nixpacks -print -quit)"
  [ -n "$source_binary" ] || fail 'Nixpacks archive did not contain its binary.'
  install -m 0755 "$source_binary" "$NIXPACKS.new"
  mv -f "$NIXPACKS.new" "$NIXPACKS"
  rm -rf "$temp"
  trap - EXIT
fi
"$NIXPACKS" --version 2>/dev/null | grep -Fq "$NIXPACKS_VERSION" || fail 'Nixpacks is not the pinned version.'
pull_if_missing(){
  local image="$1" arch
  arch="$(docker image inspect --format '{{.Architecture}}' "$image" 2>/dev/null || true)"
  [ "$arch" = arm64 ] || docker pull --platform linux/arm64 "$image" >/dev/null || fail "could not pull an ARM64 image for $image."
  arch="$(docker image inspect --format '{{.Architecture}}' "$image" 2>/dev/null || true)"
  [ "$arch" = arm64 ] || fail "$image is not ARM64; refusing an x86 image."
}
pull_if_missing 'moby/buildkit:v0.35.0'
pull_if_missing 'ghcr.io/railwayapp/nixpacks:ubuntu-1745885067'
pull_if_missing 'node:22-alpine'
pull_if_missing 'python:3.12-alpine'
pull_if_missing 'python:3.12-slim'
source_sha="$(sha256sum Dockerfile.egress mesh/egress_proxy.py mesh/ingress_proxy.py mesh/proxy_bundle.py mesh/.dockerignore | sha256sum | cut -d' ' -f1)"
image_sha="$(docker image inspect --format '{{ index .Config.Labels "sandbox-mesh.source-sha" }}' sandbox-mesh/egress-proxy:1 2>/dev/null || true)"
if [ "$image_sha" != "$source_sha" ] || [ "$(docker image inspect --format '{{.Architecture}}' sandbox-mesh/egress-proxy:1 2>/dev/null || true)" != arm64 ]; then
  docker build --platform linux/arm64 --network none --pull=false --label "sandbox-mesh.source-sha=$source_sha" --tag sandbox-mesh/egress-proxy:1 --file "$ROOT/Dockerfile.egress" "$ROOT/mesh" >/dev/null || fail 'could not build the project-owned egress proxy image.'
fi
[ "$(docker image inspect --format '{{.Architecture}}' sandbox-mesh/egress-proxy:1 2>/dev/null || true)" = arm64 ] || fail 'egress proxy image is not ARM64.'
BUILDER='sandbox-mesh-builder'
BUILD_CONTAINER='buildx_buildkit_sandbox-mesh-builder0'
BUILDKIT_IMAGE='moby/buildkit:v0.35.0'
BUILDER_PROFILE="$( { sha256sum buildkitd.toml; printf '%s\n' "$BUILDX_VERSION" "$BUILDKIT_IMAGE" "$ROOT/buildkitd.toml"; } | sha256sum | cut -d' ' -f1 )"
previous_builder_profile="$(cat "$ROOT/data/buildx.sha" 2>/dev/null || true)"
if docker buildx inspect "$BUILDER" >/dev/null 2>&1 && [ "$previous_builder_profile" != "$BUILDER_PROFILE" ]; then
  docker buildx rm "$BUILDER" >/dev/null || fail 'could not replace the outdated project Buildx builder.'
fi
if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
  docker buildx create --name "$BUILDER" --driver docker-container \
    --buildkitd-config "$ROOT/buildkitd.toml" \
    --driver-opt "image=$BUILDKIT_IMAGE" \
    --driver-opt network=bridge \
    --driver-opt default-load=true \
    --driver-opt memory=1610612736 \
    --driver-opt memory-swap=1610612736 \
    --driver-opt cpu-period=100000 \
    --driver-opt cpu-quota=70000 \
    --driver-opt env.HTTP_PROXY=http://mesh-egress:8765 \
    --driver-opt env.HTTPS_PROXY=http://mesh-egress:8765 \
    --driver-opt env.http_proxy=http://mesh-egress:8765 \
    --driver-opt env.https_proxy=http://mesh-egress:8765 \
    --driver-opt '"env.NO_PROXY=localhost,127.0.0.1,::1,mesh-egress"' \
    --driver-opt '"env.no_proxy=localhost,127.0.0.1,::1,mesh-egress"' >/dev/null || fail 'could not create the resource-limited Buildx builder.'
fi
docker buildx inspect "$BUILDER" --bootstrap >/dev/null || fail 'the pinned Buildx builder could not start.'
container="$(docker ps -aq --filter "name=^/${BUILD_CONTAINER}$")"
[ -n "$container" ] || fail 'the expected BuildKit container was not created.'
builder_limits="$(docker inspect --format '{{.HostConfig.Memory}}|{{.HostConfig.CpuPeriod}}|{{.HostConfig.CpuQuota}}|{{.HostConfig.PidsLimit}}' "$BUILD_CONTAINER")"
if [ "$builder_limits" != '1610612736|100000|70000|128' ]; then
  docker update --memory 1610612736 --memory-swap 1610612736 --cpu-period 100000 --cpu-quota 70000 --pids-limit 128 "$BUILD_CONTAINER" >/dev/null || fail 'could not apply the BuildKit CPU, memory, and PID limits.'
  builder_limits="$(docker inspect --format '{{.HostConfig.Memory}}|{{.HostConfig.CpuPeriod}}|{{.HostConfig.CpuQuota}}|{{.HostConfig.PidsLimit}}' "$BUILD_CONTAINER")"
fi
[ "$builder_limits" = '1610612736|100000|70000|128' ] || fail 'BuildKit resource limits did not verify.'
if docker network inspect bridge --format '{{json .Containers}}' 2>/dev/null | grep -q "$container"; then
  docker network disconnect bridge "$BUILD_CONTAINER" >/dev/null || fail 'could not detach the BuildKit container from unrestricted bridge networking.'
fi
attached_networks="$(docker inspect --format '{{range $name, $value := .NetworkSettings.Networks}}{{$name}} {{end}}' "$BUILD_CONTAINER")"
[ -z "$attached_networks" ] || fail 'BuildKit must be disconnected when it is idle.'
unit="$ROOT/systemd/sandbox-mesh.service"
link="$HOME/.config/systemd/user/sandbox-mesh.service"
mkdir -p "$(dirname "$link")"
if [ -L "$link" ]; then
  [ "$(readlink -f "$link")" = "$unit" ] || fail 'a different sandbox-mesh.service is already linked in the user systemd manager.'
elif [ -e "$link" ]; then
  fail 'a non-symlink sandbox-mesh.service already exists in the user systemd directory.'
else
  systemctl --user link "$unit" >/dev/null || fail 'could not link the project-owned systemd unit.'
fi
systemctl --user daemon-reload
"$python" - <<'PY' > "$ROOT/data/env-change.tmp" || fail 'could not compare the systemd manager environment.'
import os, subprocess
out=subprocess.check_output(['systemctl','--user','show-environment'],text=True)
values={line.split('=',1)[0]:line.split('=',1)[1] for line in out.splitlines() if '=' in line}
changed=any(values.get(key)!=os.environ.get(key) for key in ('SANDBOX_MESH_TOKEN','GITHUB_TOKEN'))
print('1' if changed else '0')
PY
env_changed="$(cat "$ROOT/data/env-change.tmp")"
rm -f "$ROOT/data/env-change.tmp"
if [ "$env_changed" = 1 ]; then
  systemctl --user import-environment SANDBOX_MESH_TOKEN >/dev/null || fail 'could not pass the bearer token to systemd without writing it to a file.'
  if [ -n "${GITHUB_TOKEN:-}" ]; then systemctl --user import-environment GITHUB_TOKEN >/dev/null; else systemctl --user unset-environment GITHUB_TOKEN >/dev/null 2>&1 || true; fi
fi
fingerprint="$( { find mesh scripts systemd tests -type f -print0 | sort -z | xargs -0 sha256sum; sha256sum config.yaml buildkitd.toml install.sh demo.sh Dockerfile.egress; } | sha256sum | cut -d' ' -f1 )"
previous="$(cat "$ROOT/data/install.sha" 2>/dev/null || true)"
if systemctl --user is-active --quiet sandbox-mesh.service; then
  if [ "$previous" != "$fingerprint" ] || [ "$env_changed" = 1 ]; then
    systemctl --user restart sandbox-mesh.service || fail 'the updated control plane did not restart.'
  fi
else
  systemctl --user enable --now sandbox-mesh.service || fail 'the control plane could not start; check systemctl --user status sandbox-mesh.service.'
fi
if [ "$previous" != "$fingerprint" ]; then
  printf '%s\n' "$fingerprint" > "$ROOT/data/install.sha"
  chmod 600 "$ROOT/data/install.sha"
fi
printf '%s\n' "$BUILDER_PROFILE" > "$ROOT/data/buildx.sha.new"
if ! cmp -s "$ROOT/data/buildx.sha.new" "$ROOT/data/buildx.sha" 2>/dev/null; then mv -f "$ROOT/data/buildx.sha.new" "$ROOT/data/buildx.sha"; else rm -f "$ROOT/data/buildx.sha.new"; fi
"$python" - <<'PY' || fail 'the control plane did not pass its authenticated local health check.'
import json, os, time, urllib.error, urllib.request
request=urllib.request.Request('http://127.0.0.1:8787/api/health',headers={'Authorization':'Bearer '+os.environ['SANDBOX_MESH_TOKEN']})
for _ in range(30):
 try:
  with urllib.request.urlopen(request,timeout=2) as response:
   data=json.loads(response.read())
  if data.get('status')=='ok' and data.get('driver')=='docker' and data.get('concurrency_cap')==1:
   print('Sandbox Mesh install verified.')
   raise SystemExit(0)
 except (OSError,urllib.error.URLError,urllib.error.HTTPError,ValueError):
  time.sleep(1)
raise SystemExit(1)
PY
if "$python" -m mesh.tailscale; then
  :
else
  printf 'Warning: control plane remains loopback-only until Tailscale Serve can safely route HTTPS port 8787.\n' >&2
  printf 'If Tailscale reports missing operator permission, enable it with: sudo tailscale set --operator=$USER\n' >&2
fi

from __future__ import annotations

import json
import shutil
import subprocess
import sys


PORT = 8787
TARGET = "http://127.0.0.1:8787"


class TailscaleServeError(RuntimeError):
    pass


def _run(binary: str, arguments: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([binary, *arguments], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False, close_fds=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TailscaleServeError("Tailscale Serve could not be inspected or configured") from exc


def _contains_port(value, port: int) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            text = str(key)
            if text == str(port) or text.endswith(f":{port}"):
                return True
            if _contains_port(child, port):
                return True
    elif isinstance(value, list):
        return any(_contains_port(child, port) for child in value)
    return False


def _key_uses_port(value, port: int) -> bool:
    text = str(value)
    return text == str(port) or text.endswith(f":{port}")


def _contains_target(value, target: str) -> bool:
    if isinstance(value, dict):
        return any(_contains_target(child, target) for child in value.values())
    if isinstance(value, list):
        return any(_contains_target(child, target) for child in value)
    return isinstance(value, str) and value.rstrip("/") == target


def _funnel_on_port(config: dict, port: int) -> bool:
    allowed = config.get("AllowFunnel", {})
    if allowed is True:
        return True
    if not isinstance(allowed, (dict, list)):
        return False
    def scan(value, port_context: bool = False) -> bool:
        if isinstance(value, dict):
            for key, child in value.items():
                key_has_port = str(key) == str(port) or str(key).endswith(f":{port}")
                if key_has_port and child is True:
                    return True
                if scan(child, key_has_port):
                    return True
        elif isinstance(value, list):
            return any(scan(child, port_context) for child in value)
        return port_context and value is True
    return scan(allowed)


def _status(binary: str) -> dict:
    result = _run(binary, ["serve", "status", "--json"])
    if result.returncode:
        raise TailscaleServeError("could not read the existing Tailscale Serve configuration")
    try:
        config = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise TailscaleServeError("Tailscale Serve returned invalid status JSON; existing routes were left untouched") from exc
    if not isinstance(config, dict):
        raise TailscaleServeError("Tailscale Serve status has an unexpected shape")
    return config


def ensure_private_serve() -> str:
    binary = shutil.which("tailscale")
    if not binary:
        raise TailscaleServeError("Tailscale CLI is unavailable; the control plane remains loopback-only")
    config = _status(binary)
    if _funnel_on_port(config, PORT):
        raise TailscaleServeError("port 8787 is configured for Tailscale Funnel; refusing to change a public route")
    web = config.get("Web", {})
    tcp = config.get("TCP", {})
    if not isinstance(web, dict) or not isinstance(tcp, dict):
        raise TailscaleServeError("Tailscale Serve status has an unexpected route table")
    web_conflicts = [key for key in web if _key_uses_port(key, PORT)]
    tcp_conflicts = [key for key in tcp if _key_uses_port(key, PORT)]
    if tcp_conflicts:
        raise TailscaleServeError("port 8787 already has a non-HTTP Tailscale route; existing configuration was left untouched")
    if web_conflicts:
        if len(web_conflicts) == 1 and _contains_target(web[web_conflicts[0]], TARGET):
            return "Tailscale Serve already routes HTTPS port 8787 to the loopback control plane."
        raise TailscaleServeError("HTTPS port 8787 already has a different Tailscale route; existing configuration was left untouched")
    result = _run(binary, ["serve", "--bg", "--https=8787", TARGET])
    if result.returncode:
        raise TailscaleServeError("Tailscale Serve could not add the private HTTPS route; existing configuration was left untouched")
    verified = _status(binary)
    if _funnel_on_port(verified, PORT):
        raise TailscaleServeError("Tailscale Funnel became enabled on port 8787; refusing to report a private route")
    verified_web = verified.get("Web", {})
    if not isinstance(verified_web, dict) or not any(_key_uses_port(key, PORT) and _contains_target(value, TARGET) for key, value in verified_web.items()):
        raise TailscaleServeError("the HTTPS tailnet route did not verify after configuration")
    return "Tailscale Serve is configured privately on HTTPS port 8787."


def main() -> int:
    try:
        print(ensure_private_serve())
        return 0
    except TailscaleServeError as exc:
        print(f"Tailscale Serve not changed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

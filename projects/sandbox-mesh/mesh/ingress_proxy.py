from __future__ import annotations

import http.client
import http.server
import os
import re
import socketserver


UPSTREAM_HOST = os.environ.get("SANDBOX_MESH_UPSTREAM_HOST", "")
UPSTREAM_PORT = int(os.environ.get("SANDBOX_MESH_UPSTREAM_PORT", "0"))
INGRESS_PORT = int(os.environ.get("SANDBOX_MESH_INGRESS_PORT", "8081"))
MAX_BODY = 8_388_608
BLOCKED = {"host", "authorization", "cookie", "proxy-authorization", "connection", "keep-alive", "upgrade", "transfer-encoding", "content-length", "proxy-connection"}
METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SandboxMeshEdge"
    sys_version = ""

    def log_message(self, format_string: str, *args) -> None:
        status = args[1] if len(args) > 1 else "-"
        print(f"ingress status={status}", flush=True)

    def _error(self, code: int, message: str) -> None:
        body = (message + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def _proxy(self) -> None:
        if self.command not in METHODS or not UPSTREAM_HOST or not 1 <= UPSTREAM_PORT <= 65535:
            self._error(502, "Sandbox upstream is unavailable")
            return
        if self.headers.get("Upgrade") or self.path.startswith(("http://", "https://", "//")):
            self._error(501, "This proxy accepts HTTP requests only")
            return
        if self.headers.get("Transfer-Encoding"):
            self._error(501, "Chunked request bodies are not supported")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(400, "Invalid content length")
            return
        if length < 0 or length > MAX_BODY:
            self._error(413, "Request body is too large")
            return
        body = self.rfile.read(length) if length else None
        connection_tokens = {item.strip().lower() for item in self.headers.get("Connection", "").split(",") if item.strip()}
        forwarded = {}
        for key, value in self.headers.items():
            lowered = key.lower()
            if lowered in BLOCKED or lowered in connection_tokens or lowered.startswith("x-forwarded-"):
                continue
            forwarded[key] = value
        forwarded["Host"] = f"127.0.0.1:{UPSTREAM_PORT}"
        forwarded["X-Forwarded-For"] = self.client_address[0]
        forwarded["X-Forwarded-Prefix"] = self.headers.get("X-Forwarded-Prefix", "")
        forwarded["Connection"] = "close"
        connection = None
        headers_sent = False
        try:
            connection = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=15)
            connection.request(self.command, self.path, body=body, headers=forwarded)
            response = connection.getresponse()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() in {"connection", "keep-alive", "transfer-encoding", "upgrade", "proxy-authenticate", "proxy-authorization"}:
                    continue
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            headers_sent = True
            if self.command != "HEAD":
                while True:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (OSError, http.client.HTTPException) as exc:
            if not headers_sent:
                self._error(502, f"Sandbox upstream failed ({type(exc).__name__})")
        finally:
            if connection is not None:
                connection.close()

    do_GET = _proxy
    do_HEAD = _proxy
    do_POST = _proxy
    do_PUT = _proxy
    do_PATCH = _proxy
    do_DELETE = _proxy
    do_OPTIONS = _proxy

    def do_CONNECT(self) -> None:
        self._error(405, "CONNECT is not supported")

    def do_TRACE(self) -> None:
        self._error(405, "TRACE is not supported")


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def create_server():
    if not 1 <= INGRESS_PORT <= 65535:
        raise ValueError("edge ingress port is invalid")
    return ThreadedServer(("0.0.0.0", INGRESS_PORT), Handler)


def main() -> None:
    create_server().serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()

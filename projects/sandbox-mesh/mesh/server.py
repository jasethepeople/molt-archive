from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import http.cookies
import http.server
import json
import os
import pathlib
import re
import select
import signal
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass

from .config import ConfigError, load_config
from .detector import DetectionError
from .driver import DriverError
from .docker_driver import DockerDriver
from .git_source import GitSource
from .orchestrator import Orchestrator
from .repository import RepositoryError
from .state import CapacityError, StateError, StateStore


SANDBOX_PATH = re.compile(r"^/s/([0-9a-f]{12})/([A-Za-z0-9_-]{43})(?:/(.*))?$")
OWNER_SANDBOX_PATH = re.compile(r"^/s/([0-9a-f]{12})/?$")
API_ID_PATH = re.compile(r"^/api/sandboxes/([0-9a-f]{12})(?:/(logs))?$")
DEMO_PATH = re.compile(r"^/api/demo/([a-z0-9-]{1,32})$")


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str]


def _sandbox_html(body: bytes, prefix: str) -> bytes:
    text = body.decode("utf-8", "replace")
    text = re.sub(r"(?i)(\b(?:href|src|action|poster|data-src)\s*=\s*)([\"'])/(?!/)", lambda match: match.group(1) + match.group(2) + prefix + "/", text)
    script = """<script>(function(){const p=""" + json.dumps(prefix) + """;const o=location.protocol+"//"+location.host;function r(u){if(u instanceof URL)u=u.href;if(typeof u!=="string")return u;if(u.startsWith("/")&&!u.startsWith("//"))return p+u;if(u.startsWith(o+"/")&&!u.startsWith(o+p+"/"))return p+u.slice(o.length);return u}const f=window.fetch;window.fetch=function(i,n){if(i instanceof Request){const u=r(i.url);if(u!==i.url)i=new Request(u,i)}else i=r(i);return f.call(this,i,n)};const x=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u){arguments[1]=r(u);return x.apply(this,arguments)}})();</script>"""
    injected = f'<base href="{prefix}/">' + script
    head = re.search(r"<head\b[^>]*>", text, re.IGNORECASE)
    if head:
        text = text[:head.end()] + injected + text[head.end():]
    else:
        text = injected + text
    return text.encode("utf-8")


class AuthManager:
    def __init__(self, token: str, session_ttl: int):
        if not isinstance(token, str) or len(token) < 32 or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise RuntimeError("SANDBOX_MESH_TOKEN must be set to at least 32 printable non-space characters")
        self._token = token.encode("utf-8")
        self.session_ttl = session_ttl
        self._sessions: dict[str, tuple[str, float]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _cookie(headers) -> str | None:
        raw = headers.get("Cookie", "")
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(raw)
        except http.cookies.CookieError:
            return None
        morsel = cookie.get("sm_session")
        return morsel.value if morsel else None

    def bearer_valid(self, headers) -> bool:
        value = headers.get("Authorization", "")
        if not value.startswith("Bearer "):
            return False
        candidate = value[7:].encode("utf-8", "ignore")
        return hmac.compare_digest(candidate, self._token)

    def create(self) -> tuple[str, str]:
        session_id = uuid.uuid4().hex + uuid.uuid4().hex
        csrf = uuid.uuid4().hex + uuid.uuid4().hex
        with self._lock:
            self._sessions[session_id] = (csrf, time.time() + self.session_ttl)
        return session_id, csrf

    def validate(self, headers) -> tuple[str, str] | None:
        session_id = self._cookie(headers)
        if not session_id:
            return None
        with self._lock:
            value = self._sessions.get(session_id)
            if not value:
                return None
            if value[1] <= time.time():
                self._sessions.pop(session_id, None)
                return None
            return session_id, value[0]

    def csrf_valid(self, session: tuple[str, str] | None, headers) -> bool:
        if not session:
            return False
        supplied = headers.get("X-CSRF-Token", "").encode("utf-8", "ignore")
        return hmac.compare_digest(supplied, session[1].encode("utf-8"))

    def revoke(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def session_state(self, headers) -> dict:
        session = self.validate(headers)
        return {"authenticated": bool(session), "csrf": session[1] if session else None}


class APIApplication:
    def __init__(self, project_root: pathlib.Path, config: dict, orchestrator: Orchestrator, auth: AuthManager):
        self.root = project_root.resolve()
        self.config = config
        self.orchestrator = orchestrator
        self.auth = auth
        self.index = (self.root / "mesh" / "web" / "index.html").read_bytes()

    @staticmethod
    def _json(status: int, value: dict, headers: dict[str, str] | None = None) -> Response:
        result = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}
        result.update(headers or {})
        return Response(status, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), result)

    @staticmethod
    def _origin_allowed(headers) -> bool:
        origin = headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urllib.parse.urlsplit(origin)
            host = headers.get("Host", "")
            return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == host.lower() and not parsed.path.strip("/") and not parsed.query and not parsed.fragment
        except ValueError:
            return False

    def _public_record(self, record: dict) -> dict:
        identifier = record["id"]
        return {
            "id": identifier,
            "repo_url": record.get("url", ""),
            "commit_sha": record.get("commit_sha"),
            "status": record.get("status"),
            "queue_position": getattr(self.orchestrator, "queue_position", lambda _: None)(identifier),
            "queue_reason": record.get("queue_reason"),
            "agent_status": record.get("agent_status"),
            "agent_last_heartbeat": record.get("agent_last_heartbeat"),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "expires_at": record.get("expires_at"),
            "build_type": record.get("build_kind"),
            "app_port": record.get("app_port"),
            "proxy_url": record.get("proxy_url"),
            "error": record.get("error"),
            "stats": self.orchestrator.stats(identifier) if record.get("status") == "running" else {},
        }

    def dispatch(self, method: str, raw_path: str, headers, body: bytes) -> Response:
        parsed = urllib.parse.urlsplit(raw_path)
        path = parsed.path
        if method == "GET" and path == "/":
            return Response(200, self.index, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"})
        if method == "GET" and path == "/api/session":
            return self._json(200, self.auth.session_state(headers))
        if method == "GET" and path == "/api/health":
            if not self.auth.bearer_valid(headers) and not self.auth.validate(headers):
                return self._json(401, {"error": "authentication required"})
            return self._json(200, {"status": "ok", "host": self.config["runtime"]["host_id"], "driver": "docker", "concurrency_cap": self.orchestrator.store.concurrency_cap, "active": self.orchestrator.store.active_count(), "queued": len(self.orchestrator.store.queued())})
        if method == "POST" and path == "/api/login":
            if not self._origin_allowed(headers):
                return self._json(403, {"error": "cross-origin login is not allowed"})
            if not self.auth.bearer_valid(headers):
                return self._json(401, {"error": "invalid bearer token"})
            session_id, csrf = self.auth.create()
            secure = headers.get("X-Forwarded-Proto", "").lower() == "https"
            cookie = f"sm_session={session_id}; HttpOnly; SameSite=Strict; Path=/; Max-Age={self.auth.session_ttl}" + ("; Secure" if secure else "")
            return self._json(200, {"authenticated": True, "csrf": csrf}, {"Set-Cookie": cookie})

        session = self.auth.validate(headers)
        if not session:
            return self._json(401, {"error": "authentication required"})
        if not self._origin_allowed(headers):
            return self._json(403, {"error": "cross-origin requests are not allowed"})
        if method in {"POST", "DELETE", "PUT", "PATCH"} and not self.auth.csrf_valid(session, headers):
            return self._json(403, {"error": "invalid CSRF token"})

        if method == "POST" and path == "/api/logout":
            self.auth.revoke(session[0])
            return self._json(200, {"ok": True}, {"Set-Cookie": "sm_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
        if method == "GET" and path == "/api/sandboxes":
            return self._json(200, {"items": [self._public_record(item) for item in self.orchestrator.list()]})
        if method == "GET" and path == "/api/scheduler":
            return self._json(200, self.orchestrator.scheduler_snapshot())
        if method == "GET" and path == "/api/resources":
            try:
                return self._json(200, self.orchestrator.resource_snapshot())
            except DriverError as exc:
                return self._json(503, {"error": str(exc)})
        if method == "GET" and path == "/api/queue":
            items = []
            for item in self.orchestrator.store.queued():
                public = self._public_record(item)
                public["queue_position"] = self.orchestrator.queue_position(item["id"])
                items.append(public)
            return self._json(200, {"items": items, "scheduler": self.orchestrator.scheduler_snapshot()})
        demo = DEMO_PATH.fullmatch(path)
        if method == "POST" and demo:
            try:
                record = self.orchestrator.submit_demo(demo.group(1))
                return self._json(202, {"item": self._public_record(record)})
            except (RepositoryError, CapacityError, StateError, RuntimeError) as exc:
                return self._json(409 if isinstance(exc, CapacityError) else 400, {"error": str(exc)})
        if method == "POST" and path == "/api/sandboxes":
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                return self._json(400, {"error": "request body must be valid JSON"})
            if not isinstance(payload, dict) or set(payload) != {"url"} or not isinstance(payload["url"], str):
                return self._json(400, {"error": "send exactly one GitHub repository URL in the url field"})
            try:
                record = self.orchestrator.submit(payload["url"])
                return self._json(202, {"item": self._public_record(record)})
            except CapacityError as exc:
                return self._json(409, {"error": str(exc)})
            except (RepositoryError, StateError, RuntimeError) as exc:
                return self._json(400, {"error": str(exc)})
        match = API_ID_PATH.fullmatch(path)
        if match:
            identifier, suffix = match.groups()
            record = self.orchestrator.get(identifier)
            if not record:
                return self._json(404, {"error": "sandbox not found"})
            if method == "GET" and suffix == "logs":
                return self._json(200, {"logs": self.orchestrator.logs(identifier)})
            if method == "GET" and suffix is None:
                return self._json(200, {"item": self._public_record(record)})
            if method == "POST" and suffix is None:
                return self._json(405, {"error": "action must be stop, restart, or destroy"})
            if method == "POST" and suffix == "logs":
                return self._json(405, {"error": "logs are read-only"})
            if suffix is None and method == "DELETE":
                try:
                    result = self.orchestrator.destroy(identifier)
                    return self._json(200, {"item": self._public_record(result)})
                except (KeyError, StateError) as exc:
                    return self._json(409, {"error": str(exc)})
        action_match = re.fullmatch(r"/api/sandboxes/([0-9a-f]{12})/(stop|restart|destroy|cancel|crash-app|kill-agent)", path)
        if method == "POST" and action_match:
            identifier, action = action_match.groups()
            try:
                result = getattr(self.orchestrator, action.replace("-", "_"))(identifier)
                return self._json(200, {"item": self._public_record(result)})
            except KeyError as exc:
                return self._json(404, {"error": str(exc)})
            except (StateError, CapacityError, DriverError) as exc:
                return self._json(409, {"error": str(exc)})
        return self._json(404, {"error": "not found"})

    def proxy(self, handler, method: str, raw_path: str) -> None:
        parsed = urllib.parse.urlsplit(raw_path)
        match = SANDBOX_PATH.fullmatch(parsed.path)
        if not match:
            handler.send_error(404)
            return
        identifier, ticket, remainder = match.groups()
        record = self.orchestrator.get(identifier)
        supplied = hashlib.sha256(ticket.encode("ascii")).hexdigest()
        expected = record.get("proxy_ticket_hash") if record else None
        if not record or record.get("status") != "running" or not expected or not hmac.compare_digest(supplied, expected):
            handler.send_error(404, "sandbox capability is invalid or expired")
            return
        origin = handler.headers.get("Origin")
        if origin and origin != "null":
            handler.send_error(403, "sandbox apps may only be requested from their isolated origin")
            return
        prefix = f"/s/{identifier}/{ticket}"
        cors = {"Access-Control-Allow-Origin": "null", "Vary": "Origin"}
        if method == "OPTIONS":
            handler.send_response(204)
            for key, value in cors.items():
                handler.send_header(key, value)
            handler.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS")
            handler.send_header("Access-Control-Allow-Headers", "Content-Type, X-Requested-With")
            handler.send_header("Access-Control-Max-Age", "600")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            handler.close_connection = True
            return
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
            handler.send_error(405)
            return
        if handler.headers.get("Upgrade"):
            handler.send_error(501, "application WebSocket proxying is not enabled in Phase 1")
            return
        if handler.headers.get("Transfer-Encoding"):
            handler.send_error(501, "chunked proxy request bodies are not supported")
            return
        try:
            length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            handler.send_error(400, "invalid content length")
            return
        if length < 0 or length > 8_388_608:
            handler.send_error(413, "proxy request is too large")
            return
        request_body = handler.rfile.read(length) if length else None
        target_path = "/" + (remainder or "") + (("?" + parsed.query) if parsed.query else "")
        blocked = {"host", "authorization", "cookie", "proxy-authorization", "connection", "keep-alive", "upgrade", "transfer-encoding", "content-length", "origin", "referer"}
        connection_tokens = {part.strip().lower() for part in handler.headers.get("Connection", "").split(",") if part.strip()}
        forwarded = {}
        for key, value in handler.headers.items():
            lowered = key.lower()
            if lowered in blocked or lowered in connection_tokens or lowered.startswith("x-forwarded-"):
                continue
            forwarded[key] = value
        try:
            host, port = self.orchestrator.proxy_target(identifier)
        except (KeyError, RuntimeError):
            handler.send_error(404, "sandbox is not running")
            return
        forwarded["Host"] = f"127.0.0.1:{port}"
        forwarded["X-Forwarded-Prefix"] = prefix
        forwarded["Accept-Encoding"] = "identity"
        forwarded["Connection"] = "close"
        connection = None
        headers_sent = False
        try:
            connection = http.client.HTTPConnection(host, port, timeout=15)
            connection.request(method, target_path, body=request_body, headers=forwarded)
            response = connection.getresponse()
            response_headers = response.getheaders()
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type == "application/xhtml+xml":
                handler.send_error(502, "XHTML documents are not supported by the safe proxy")
                return
            is_html = method != "HEAD" and response.status not in {204, 304} and content_type == "text/html"
            host = handler.headers.get("Host", "localhost:8787")
            scheme = handler.headers.get("X-Forwarded-Proto", "http")
            if scheme not in {"http", "https"} or not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", host):
                handler.send_error(400, "invalid proxy host")
                return
            origin_url = f"{scheme}://{host}"
            csp = f"default-src 'none'; sandbox allow-scripts allow-forms allow-downloads; base-uri {origin_url}; script-src {origin_url} 'unsafe-inline' 'unsafe-eval' blob: data:; style-src {origin_url} 'unsafe-inline'; img-src {origin_url} data: blob:; font-src {origin_url} data:; connect-src {origin_url}; frame-src {origin_url} data: blob:; media-src {origin_url} blob:; worker-src {origin_url} blob:; form-action {origin_url}; frame-ancestors 'none'; object-src 'none'"
            html_body = None
            if is_html:
                encoding = response.getheader("Content-Encoding", "identity").lower()
                if encoding not in {"", "identity"}:
                    handler.send_error(502, "compressed HTML responses cannot be safely sandboxed")
                    return
                html_body = response.read(4_194_305)
                if len(html_body) > 4_194_304:
                    handler.send_error(502, "HTML response exceeds the safe rewrite limit")
                    return
                html_body = _sandbox_html(html_body, prefix)
            handler.send_response(response.status, response.reason)
            for key, value in response_headers:
                lower = key.lower()
                if lower in {"connection", "keep-alive", "transfer-encoding", "upgrade", "proxy-authenticate", "proxy-authorization", "set-cookie", "content-security-policy", "content-length"}:
                    continue
                if lower == "location" and value.startswith("/") and not value.startswith("//"):
                    value = prefix + value
                handler.send_header(key, value)
            for key, value in cors.items():
                handler.send_header(key, value)
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Referrer-Policy", "no-referrer")
            handler.send_header("X-Content-Type-Options", "nosniff")
            handler.send_header("Content-Security-Policy", csp)
            if is_html:
                handler.send_header("Content-Length", str(len(html_body)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.close_connection = True
            headers_sent = True
            if html_body is not None:
                handler.wfile.write(html_body)
                handler.wfile.flush()
            elif method != "HEAD":
                while True:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
        except (OSError, http.client.HTTPException) as exc:
            if not headers_sent:
                handler.send_error(502, f"sandbox proxy failed ({type(exc).__name__})")
        finally:
            if connection is not None:
                connection.close()


class RequestHandler(http.server.BaseHTTPRequestHandler):
    server_version = "SandboxMesh"
    sys_version = ""

    @property
    def app(self) -> APIApplication:
        return self.server.app

    def log_message(self, format_string: str, *args) -> None:
        status = args[1] if len(args) > 1 else "-"
        print(f"http request status={status}", flush=True)

    def _send(self, response: Response) -> None:
        self.send_response(response.status)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        limit = int(self.app.config["control_plane"]["max_request_bytes"])
        if length < 0 or length > limit:
            raise ValueError("request body exceeds the configured size limit")
        return self.rfile.read(length) if length else b""

    def _dispatch(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if SANDBOX_PATH.fullmatch(path):
            self.app.proxy(self, self.command, self.path)
            return
        owner_route = OWNER_SANDBOX_PATH.fullmatch(path)
        if owner_route:
            session = self.app.auth.validate(self.headers)
            if not session:
                self._send(self.app._json(401, {"error": "authentication required"}))
                return
            if not self.app._origin_allowed(self.headers):
                self._send(self.app._json(403, {"error": "cross-origin requests are not allowed"}))
                return
            record = self.app.orchestrator.get(owner_route.group(1))
            if not record or record.get("status") != "running" or not record.get("proxy_url"):
                self._send(self.app._json(404, {"error": "sandbox is not running"}))
                return
            self._send(Response(303, b"", {"Location": record["proxy_url"], "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}))
            return
        if path.startswith("/s/"):
            self._send(self.app._json(404, {"error": "sandbox route not found"}))
            return
        try:
            body = self._body() if self.command in {"POST", "PUT", "PATCH", "DELETE"} else b""
        except (ValueError, OverflowError):
            self._send(self.app._json(413, {"error": "request body exceeds 32 KiB"}))
            return
        self._send(self.app.dispatch(self.command, self.path, self.headers, body))

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self._send(self.app._json(405, {"error": "cross-origin requests are disabled"}))

    def _read_ws_frame(self):
        def read_exact(size: int) -> bytes | None:
            parts = bytearray()
            while len(parts) < size:
                chunk = self.connection.recv(size - len(parts))
                if not chunk:
                    return None
                parts.extend(chunk)
            return bytes(parts)

        header = read_exact(2)
        if not header:
            return None
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        if length == 126:
            extended = read_exact(2)
            if extended is None:
                return None
            length = int.from_bytes(extended, "big")
        elif length == 127:
            extended = read_exact(8)
            if extended is None:
                return None
            length = int.from_bytes(extended, "big")
        if length > 125:
            return None
        masked = bool(header[1] & 0x80)
        mask = read_exact(4) if masked else b""
        payload = read_exact(length) if length else b""
        if payload is None or (masked and mask is None):
            return None
        if not masked:
            return None
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        if opcode == 9:
            size = len(payload)
            self.connection.sendall(bytes([0x8A, size]) + payload)
            return True
        return opcode != 8

    def do_ws(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        match = re.fullmatch(r"/api/sandboxes/([0-9a-f]{12})/logs/ws", parsed.path)
        session = self.app.auth.validate(self.headers)
        connection_tokens = {part.strip().lower() for part in self.headers.get("Connection", "").split(",")}
        if not match or not session or not self.app._origin_allowed(self.headers) or "upgrade" not in connection_tokens:
            self.send_error(401)
            return
        key = self.headers.get("Sec-WebSocket-Key", "")
        try:
            decoded = __import__("base64").b64decode(key, validate=True)
        except Exception:
            self.send_error(400)
            return
        if len(decoded) != 16 or self.headers.get("Sec-WebSocket-Version") != "13":
            self.send_error(400)
            return
        identifier = match.group(1)
        if not self.app.orchestrator.get(identifier):
            self.send_error(404)
            return
        accept = __import__("base64").b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
        self.send_response_only(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        self.connection.settimeout(30)
        stop = threading.Event()
        try:
            initial = self.app.orchestrator.logs(identifier)
            info = self.app.orchestrator.logs_root / f"{identifier}.log"
            offset = info.stat().st_size if info.exists() else 0
            if initial:
                _send_ws_text(self.connection, initial)
            last_ping = time.monotonic()
            for chunk in self.app.orchestrator.watch_logs(identifier, offset, stop):
                if chunk:
                    _send_ws_text(self.connection, chunk)
                now = time.monotonic()
                if now - last_ping >= 25:
                    self.connection.sendall(b"\x89\x00")
                    last_ping = now
                readable, _, _ = select.select([self.connection], [], [], 0)
                if readable and not self._read_ws_frame():
                    try:
                        self.connection.sendall(b"\x88\x00")
                    except OSError:
                        pass
                    break
        except (OSError, BrokenPipeError, KeyError, TimeoutError):
            pass
        finally:
            stop.set()

    def handle_one_request(self) -> None:
        super().handle_one_request()

    def parse_request(self) -> bool:
        ok = super().parse_request()
        if ok and self.command == "GET" and self.headers.get("Upgrade", "").lower() == "websocket":
            self.do_ws()
            return False
        return ok


class ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, app):
        self.app = app
        super().__init__(address, handler)


def _send_ws_text(connection, text: str) -> None:
    payload = text.encode("utf-8", "replace")
    if len(payload) > 1_000_000:
        payload = payload[-1_000_000:]
    first = b"\x81"
    size = len(payload)
    if size < 126:
        header = first + bytes([size])
    elif size <= 65535:
        header = first + b"\x7e" + size.to_bytes(2, "big")
    else:
        header = first + b"\x7f" + size.to_bytes(8, "big")
    connection.sendall(header + payload)


def build_application(project_root: pathlib.Path, config: dict) -> tuple[APIApplication, Orchestrator]:
    token = os.environ.get("SANDBOX_MESH_TOKEN", "")
    auth = AuthManager(token, int(config["control_plane"]["session_ttl_seconds"]))
    store = StateStore(project_root / config["control_plane"]["database_path"], int(config["runtime"]["concurrency_cap"]))
    source = GitSource(project_root, config)
    driver = DockerDriver(project_root, config)
    orchestrator = Orchestrator(project_root, config, store, source, driver)
    return APIApplication(project_root, config, orchestrator, auth), orchestrator


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default=str(pathlib.Path(__file__).resolve().parents[1]))
    parser.add_argument("--check-config", action="store_true")
    arguments = parser.parse_args()
    root = pathlib.Path(arguments.project_root).resolve(strict=True)
    try:
        config = load_config(root)
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    if arguments.check_config:
        print("Sandbox Mesh configuration is valid.")
        return
    app, orchestrator = build_application(root, config)
    orchestrator.startup_reconcile()
    orchestrator.start_background()
    server = ThreadingHTTPServer((config["control_plane"]["bind_host"], config["control_plane"]["port"]), RequestHandler, app)
    shutdown = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, name="http-server", daemon=True)
    shutdown.start()
    print("Sandbox Mesh listening on 127.0.0.1:8787; Docker is the only execution driver.", flush=True)
    stopped = threading.Event()

    def request_shutdown(signum, frame):
        if not stopped.is_set():
            stopped.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    try:
        while not stopped.wait(1):
            if not shutdown.is_alive():
                break
    finally:
        server.server_close()
        orchestrator.stop_background()


if __name__ == "__main__":
    main()

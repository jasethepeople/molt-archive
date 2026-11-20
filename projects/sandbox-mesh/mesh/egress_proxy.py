from __future__ import annotations

import ipaddress
import os
import re
import select
import socket
import socketserver
from urllib.parse import urlsplit


ALLOWLIST = {item.strip().lower().rstrip(".") for item in os.environ.get("SANDBOX_MESH_EGRESS_ALLOWLIST", "").split(",") if item.strip()}
MAX_HEADERS = 65536
MAX_BODY = 67_108_864
MAX_CLIENT_HELLO = 65536


def normalize_hostname(host: str) -> str:
    name = host.rstrip(".").encode("idna").decode("ascii").lower()
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        raise ValueError("IP-literal egress targets are not allowed")
    labels = name.split(".")
    if len(name) > 253 or not all(label and len(label) <= 63 and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) for label in labels):
        raise ValueError("destination hostname is invalid")
    return name


def resolve_public(host: str, port: int) -> list[tuple]:
    name = normalize_hostname(host)
    if name not in ALLOWLIST:
        raise ValueError("destination is not in the sandbox egress allowlist")
    results = socket.getaddrinfo(name, port, type=socket.SOCK_STREAM)
    safe = []
    for family, socktype, proto, _, address in results:
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global or ip.is_multicast or ip.is_unspecified:
            raise ValueError("destination resolves to a non-public address")
        safe.append((family, socktype, proto, address))
    if not safe:
        raise ValueError("destination did not resolve")
    return safe


def _read_exact(client: socket.socket, buffer: bytearray, size: int, wire: bytearray) -> bytes:
    while len(buffer) < size:
        block = client.recv(min(16384, size - len(buffer)))
        if not block:
            raise ValueError("incomplete TLS ClientHello")
        buffer.extend(block)
    result = bytes(buffer[:size])
    del buffer[:size]
    wire.extend(result)
    return result


def _client_hello_server_name(handshake: bytes) -> str:
    if len(handshake) < 4 or handshake[0] != 1:
        raise ValueError("TLS tunnel did not begin with a ClientHello")
    body_length = int.from_bytes(handshake[1:4], "big")
    if body_length > MAX_CLIENT_HELLO or len(handshake) < body_length + 4:
        raise ValueError("TLS ClientHello is invalid or too large")
    body = handshake[4:4 + body_length]
    cursor = 34
    if len(body) < cursor + 1:
        raise ValueError("TLS ClientHello is incomplete")
    session_length = body[cursor]
    cursor += 1 + session_length
    if cursor + 2 > len(body):
        raise ValueError("TLS ClientHello is incomplete")
    cipher_length = int.from_bytes(body[cursor:cursor + 2], "big")
    cursor += 2 + cipher_length
    if cursor + 1 > len(body):
        raise ValueError("TLS ClientHello is incomplete")
    compression_length = body[cursor]
    cursor += 1 + compression_length
    if cursor == len(body):
        raise ValueError("TLS ClientHello has no server name")
    if cursor + 2 > len(body):
        raise ValueError("TLS ClientHello is incomplete")
    extensions_length = int.from_bytes(body[cursor:cursor + 2], "big")
    cursor += 2
    end = cursor + extensions_length
    if end != len(body):
        raise ValueError("TLS ClientHello extensions are invalid")
    server_name = None
    saw_sni_extension = False
    while cursor < end:
        if cursor + 4 > end:
            raise ValueError("TLS ClientHello extension is incomplete")
        extension_type = int.from_bytes(body[cursor:cursor + 2], "big")
        extension_length = int.from_bytes(body[cursor + 2:cursor + 4], "big")
        cursor += 4
        extension_end = cursor + extension_length
        if extension_end > end:
            raise ValueError("TLS ClientHello extension length is invalid")
        extension = body[cursor:extension_end]
        cursor = extension_end
        if extension_type == 0xFE0D:
            raise ValueError("Encrypted ClientHello is not allowed through the egress proxy")
        if extension_type != 0:
            continue
        if saw_sni_extension or len(extension) < 2:
            raise ValueError("TLS ClientHello server name is invalid")
        saw_sni_extension = True
        names_length = int.from_bytes(extension[:2], "big")
        index = 2
        if names_length != len(extension) - 2:
            raise ValueError("TLS ClientHello server name list is invalid")
        while index < len(extension):
            if index + 3 > len(extension):
                raise ValueError("TLS ClientHello server name is incomplete")
            name_type = extension[index]
            name_length = int.from_bytes(extension[index + 1:index + 3], "big")
            index += 3
            name_end = index + name_length
            if name_end > len(extension):
                raise ValueError("TLS ClientHello server name length is invalid")
            if name_type == 0:
                if server_name is not None:
                    raise ValueError("TLS ClientHello has multiple host names")
                try:
                    server_name = extension[index:name_end].decode("ascii")
                except UnicodeDecodeError as exc:
                    raise ValueError("TLS ClientHello hostname is not ASCII") from exc
            index = name_end
    if server_name is None:
        raise ValueError("TLS ClientHello has no server name")
    return normalize_hostname(server_name)


def read_client_hello(client: socket.socket, initial: bytes = b"") -> tuple[str, bytes]:
    buffer = bytearray(initial)
    wire = bytearray()
    handshake = bytearray()
    expected = None
    while expected is None or len(handshake) < expected:
        record_header = _read_exact(client, buffer, 5, wire)
        record_length = int.from_bytes(record_header[3:5], "big")
        if record_header[0] != 22 or record_length == 0 or record_length > 18432:
            raise ValueError("TLS tunnel did not begin with a valid handshake record")
        handshake.extend(_read_exact(client, buffer, record_length, wire))
        if len(handshake) > MAX_CLIENT_HELLO + 4 + 18432:
            raise ValueError("TLS ClientHello is too large")
        if expected is None and len(handshake) >= 4:
            expected = 4 + int.from_bytes(handshake[1:4], "big")
            if handshake[0] != 1 or expected > MAX_CLIENT_HELLO + 4:
                raise ValueError("TLS tunnel did not begin with an acceptable ClientHello")
    server_name = _client_hello_server_name(bytes(handshake[:expected]))
    wire.extend(buffer)
    return server_name, bytes(wire)


def read_allowed_client_hello(client: socket.socket, destination_host: str, initial: bytes = b"") -> bytes:
    server_name, client_hello = read_client_hello(client, initial)
    if server_name != normalize_hostname(destination_host):
        raise ValueError("TLS server name does not match the allowlisted CONNECT target")
    return client_hello


def recv_headers(client: socket.socket) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        block = client.recv(4096)
        if not block:
            raise ValueError("incomplete proxy request")
        data.extend(block)
        if len(data) > MAX_HEADERS:
            raise ValueError("proxy headers are too large")
    return bytes(data)


def _read_buffered(client: socket.socket, buffer: bytearray, size: int) -> bytes:
    while len(buffer) < size:
        block = client.recv(min(65536, size - len(buffer)))
        if not block:
            raise ValueError("incomplete proxy request body")
        buffer.extend(block)
    result = bytes(buffer[:size])
    del buffer[:size]
    return result


def _read_line(client: socket.socket, buffer: bytearray) -> bytes:
    while True:
        index = buffer.find(b"\r\n")
        if index >= 0:
            if index > 8192:
                raise ValueError("proxy body framing line is too long")
            result = bytes(buffer[:index + 2])
            del buffer[:index + 2]
            return result
        if len(buffer) > 8192:
            raise ValueError("proxy body framing line is too long")
        block = client.recv(4096)
        if not block:
            raise ValueError("incomplete proxy body framing")
        buffer.extend(block)


def relay_chunked_body(client: socket.socket, upstream: socket.socket, remainder: bytes) -> None:
    buffer = bytearray(remainder)
    total = 0
    while True:
        line = _read_line(client, buffer)
        upstream.sendall(line)
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError as exc:
            raise ValueError("invalid chunked proxy body") from exc
        if size < 0:
            raise ValueError("invalid chunked proxy body")
        if size == 0:
            while True:
                trailer = _read_line(client, buffer)
                upstream.sendall(trailer)
                if trailer == b"\r\n":
                    return
        total += size
        if total > MAX_BODY:
            raise ValueError("proxy request body is too large")
        block = _read_buffered(client, buffer, size + 2)
        if not block.endswith(b"\r\n"):
            raise ValueError("invalid chunked proxy body")
        upstream.sendall(block)


def relay_length_body(client: socket.socket, upstream: socket.socket, remainder: bytes, length: int) -> None:
    if length < 0 or length > MAX_BODY:
        raise ValueError("proxy request body is too large")
    if len(remainder) > length:
        upstream.sendall(remainder[:length])
        return
    if remainder:
        upstream.sendall(remainder)
    remaining = length - len(remainder)
    while remaining:
        block = client.recv(min(65536, remaining))
        if not block:
            raise ValueError("incomplete proxy request body")
        upstream.sendall(block)
        remaining -= len(block)


def relay(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    while True:
        readable, _, _ = select.select(sockets, [], [], 60)
        if not readable:
            return
        for source in readable:
            target = right if source is left else left
            try:
                data = source.recv(65536)
            except OSError:
                return
            if not data:
                return
            target.sendall(data)


def connect_addresses(addresses: list[tuple]) -> socket.socket:
    last_error = None
    for family, socktype, proto, address in addresses:
        upstream = socket.socket(family, socktype, proto)
        upstream.settimeout(10)
        try:
            upstream.connect(address)
            upstream.settimeout(15)
            return upstream
        except OSError as exc:
            last_error = exc
            upstream.close()
    raise last_error or OSError("could not connect to destination")


def connect_allowed(host: str, port: int) -> socket.socket:
    return connect_addresses(resolve_public(host, port))


def respond(client: socket.socket, status: int, text: str) -> None:
    body = text.encode("utf-8", "replace")[:200]
    client.sendall(f"HTTP/1.1 {status} {'Forbidden' if status == 403 else 'Bad Gateway'}\r\nConnection: close\r\nContent-Length: {len(body)}\r\nContent-Type: text/plain\r\n\r\n".encode("ascii") + body)


class Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        upstream = None
        tunnel_established = False
        try:
            self.request.settimeout(15)
            data = recv_headers(self.request)
            header, remainder = data.split(b"\r\n\r\n", 1)
            lines = header.decode("iso-8859-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            if method.upper() == "CONNECT":
                host, port_text = target.rsplit(":", 1)
                port = int(port_text)
                host = normalize_hostname(host.strip("[]"))
                if port != 443:
                    raise ValueError("only TLS CONNECT on port 443 is allowed")
                if host not in ALLOWLIST:
                    raise ValueError("destination is not in the sandbox egress allowlist")
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                tunnel_established = True
                client_hello = read_allowed_client_hello(self.request, host, remainder)
                upstream = connect_addresses(resolve_public(host, port))
                upstream.sendall(client_hello)
                self.request.settimeout(None)
                upstream.settimeout(None)
                relay(self.request, upstream)
                return

            parsed = urlsplit(target)
            if parsed.scheme != "http" or not parsed.hostname:
                raise ValueError("only allowlisted HTTP proxy targets are supported")
            port = parsed.port or 80
            if port != 80:
                raise ValueError("only HTTP port 80 is allowed for cleartext requests")
            upstream = connect_allowed(parsed.hostname, port)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            header_lines = lines[1:]
            connection_tokens = {part.strip().lower() for line in header_lines if line.lower().startswith("connection:") for part in line.split(":", 1)[1].split(",") if part.strip()}
            filtered = []
            content_lengths = []
            chunked = False
            expect_continue = False
            for line in header_lines:
                name, separator, value = line.partition(":")
                if not separator:
                    continue
                lowered = name.strip().lower()
                if lowered in {"proxy-connection", "proxy-authorization", "connection", "host"} or lowered in connection_tokens:
                    continue
                if lowered == "content-length":
                    content_lengths.append(int(value.strip()))
                if lowered == "transfer-encoding":
                    chunked = "chunked" in value.lower()
                    if not chunked:
                        raise ValueError("unsupported transfer encoding")
                if lowered == "expect" and value.strip().lower() == "100-continue":
                    expect_continue = True
                    continue
                filtered.append(line)
            if len(set(content_lengths)) > 1:
                raise ValueError("conflicting content lengths")
            forwarded = [f"{method} {path} {version}", *filtered, f"Host: {parsed.hostname}", "Connection: close", "", ""]
            if expect_continue:
                self.request.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
            upstream.sendall("\r\n".join(forwarded).encode("iso-8859-1"))
            if chunked:
                relay_chunked_body(self.request, upstream, remainder)
            elif content_lengths:
                relay_length_body(self.request, upstream, remainder, content_lengths[0])
            elif remainder:
                raise ValueError("unframed proxy request bodies are not supported")
            self.request.settimeout(15)
            while True:
                block = upstream.recv(65536)
                if not block:
                    break
                self.request.sendall(block)
        except ValueError:
            if not tunnel_established:
                try:
                    respond(self.request, 403, "Blocked by Sandbox Mesh egress policy")
                except OSError:
                    pass
        except OSError:
            if not tunnel_established:
                try:
                    respond(self.request, 502, "Sandbox Mesh egress connection failed")
                except OSError:
                    pass
        finally:
            if upstream:
                upstream.close()


class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def create_server():
    return ThreadedServer(("0.0.0.0", 8765), Handler)


def main() -> None:
    create_server().serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()

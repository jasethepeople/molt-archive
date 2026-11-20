from __future__ import annotations

import socket
import unittest
from unittest import mock

from mesh import egress_proxy


class FragmentedSocket:
    def __init__(self, data: bytes):
        self.data = bytearray(data)

    def recv(self, size: int) -> bytes:
        if not self.data:
            return b""
        count = min(size, 3)
        result = bytes(self.data[:count])
        del self.data[:count]
        return result


def client_hello(host: str | None, extra_extensions: bytes = b"") -> bytes:
    extensions = bytearray(extra_extensions)
    if host is not None:
        encoded = host.encode("ascii")
        name = b"\x00" + len(encoded).to_bytes(2, "big") + encoded
        server_names = len(name).to_bytes(2, "big") + name
        extensions.extend(b"\x00\x00" + len(server_names).to_bytes(2, "big") + server_names)
    body = (
        b"\x03\x03" + bytes(range(32)) + b"\x00"
        + b"\x00\x02\x00\x2f" + b"\x01\x00"
        + len(extensions).to_bytes(2, "big") + extensions
    )
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    record = b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake
    return record + b"\x17\x03\x03\x00\x04data"


class EgressProxyTests(unittest.TestCase):
    def test_fragmented_client_hello_checks_exact_sni_and_preserves_following_bytes(self):
        raw = client_hello("packages.example")
        record_length = int.from_bytes(raw[3:5], "big")
        record = raw[:5 + record_length]
        with mock.patch.object(egress_proxy, "ALLOWLIST", {"packages.example"}):
            forwarded = egress_proxy.read_allowed_client_hello(FragmentedSocket(record), "packages.example")
            buffered = egress_proxy.read_allowed_client_hello(FragmentedSocket(b""), "packages.example", raw)
        self.assertEqual(forwarded, record)
        self.assertEqual(buffered, raw)

    def test_connect_target_sni_mismatch_is_rejected(self):
        with mock.patch.object(egress_proxy, "ALLOWLIST", {"allowed.example"}):
            with self.assertRaisesRegex(ValueError, "does not match"):
                egress_proxy.read_allowed_client_hello(FragmentedSocket(client_hello("other.example")), "allowed.example")

    def test_missing_sni_and_encrypted_client_hello_are_rejected(self):
        with mock.patch.object(egress_proxy, "ALLOWLIST", {"allowed.example"}):
            with self.assertRaisesRegex(ValueError, "no server name"):
                egress_proxy.read_allowed_client_hello(FragmentedSocket(client_hello(None)), "allowed.example")
            ech = b"\xfe\x0d\x00\x00"
            with self.assertRaisesRegex(ValueError, "Encrypted ClientHello"):
                egress_proxy.read_allowed_client_hello(FragmentedSocket(client_hello("allowed.example", ech)), "allowed.example")

    def test_dns_allowlist_rejects_unlisted_and_private_destinations(self):
        public_result = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with mock.patch.object(egress_proxy, "ALLOWLIST", {"allowed.example"}), mock.patch.object(egress_proxy.socket, "getaddrinfo", return_value=public_result) as lookup:
            self.assertEqual(egress_proxy.resolve_public("allowed.example", 443), [(socket.AF_INET, socket.SOCK_STREAM, 6, ("8.8.8.8", 443))])
            with self.assertRaisesRegex(ValueError, "not in the sandbox egress allowlist"):
                egress_proxy.resolve_public("other.example", 443)
            lookup.assert_called_once_with("allowed.example", 443, type=socket.SOCK_STREAM)
        private_result = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with mock.patch.object(egress_proxy, "ALLOWLIST", {"allowed.example"}), mock.patch.object(egress_proxy.socket, "getaddrinfo", return_value=private_result):
            with self.assertRaisesRegex(ValueError, "non-public address"):
                egress_proxy.resolve_public("allowed.example", 443)


if __name__ == "__main__":
    unittest.main()

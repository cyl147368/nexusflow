import socket
import socketserver
import threading
import unittest
from urllib.parse import urlsplit

from local_proxy_bridge import LocalProxyBridge, UpstreamProxy


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("socket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_headers(sock: socket.socket) -> tuple[bytes, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("socket closed")
        data += chunk
    head, remaining = data.split(b"\r\n\r\n", 1)
    return head, remaining


class _HTTPConnectHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        head, remaining = _recv_headers(self.request)
        if b"CONNECT example.com:443" not in head:
            raise AssertionError(f"CONNECT target missing: {head!r}")
        if b"Proxy-Authorization: Basic" not in head:
            raise AssertionError(f"proxy authorization missing: {head!r}")
        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        payload = remaining or self.request.recv(4096)
        self.request.sendall(b"http:" + payload)


class _Socks5Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        greeting = _recv_exact(self.request, 2)
        methods = _recv_exact(self.request, greeting[1])
        self.assertIn(0x02, methods)
        self.request.sendall(b"\x05\x02")

        version = _recv_exact(self.request, 2)
        self.assertEqual(version[0], 0x01)
        username = _recv_exact(self.request, version[1])
        password_length = _recv_exact(self.request, 1)[0]
        password = _recv_exact(self.request, password_length)
        self.assertEqual(username, b"user")
        self.assertEqual(password, b"pass")
        self.request.sendall(b"\x01\x00")

        connect_head = _recv_exact(self.request, 4)
        self.assertEqual(connect_head[:3], b"\x05\x01\x00")
        if connect_head[3] == 0x03:
            host_length = _recv_exact(self.request, 1)[0]
            host = _recv_exact(self.request, host_length)
            self.assertEqual(host, b"example.com")
            _recv_exact(self.request, 2)
        else:
            self.fail(f"unexpected address type: {connect_head[3]}")
        self.request.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
        payload = self.request.recv(4096)
        self.request.sendall(b"socks:" + payload)

    @staticmethod
    def assertIn(value: bytes, container: bytes) -> None:
        if value not in container:
            raise AssertionError(f"{value!r} not found in {container!r}")

    @staticmethod
    def assertEqual(left: bytes, right: bytes) -> None:
        if left != right:
            raise AssertionError(f"{left!r} != {right!r}")

    @staticmethod
    def fail(message: str) -> None:
        raise AssertionError(message)


class _ThreadedServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class LocalProxyBridgeTests(unittest.TestCase):
    def _start_upstream(self, handler):
        server = _ThreadedServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def _exercise_bridge(self, proxy_url: str, expected_prefix: bytes) -> None:
        bridge = LocalProxyBridge(UpstreamProxy.parse(proxy_url))
        bridge.start()
        self.addCleanup(bridge.stop)
        parsed = urlsplit(bridge.local_url)
        with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as client:
            client.sendall(
                b"CONNECT example.com:443 HTTP/1.1\r\n"
                b"Host: example.com:443\r\n\r\n"
            )
            response, remaining = _recv_headers(client)
            self.assertIn(b"200 Connection Established", response)
            client.sendall(b"ping")
            echoed = remaining or _recv_exact(client, len(expected_prefix) + 4)
            self.assertEqual(echoed, expected_prefix + b"ping")

    def test_http_upstream_with_basic_auth_is_bridged(self):
        port = self._start_upstream(_HTTPConnectHandler)
        self._exercise_bridge(
            f"http://user:pass@127.0.0.1:{port}",
            b"http:",
        )

    def test_authenticated_socks5_upstream_is_bridged(self):
        port = self._start_upstream(_Socks5Handler)
        self._exercise_bridge(
            f"socks5h://user:pass@127.0.0.1:{port}",
            b"socks:",
        )

    def test_proxy_parser_rejects_missing_port(self):
        with self.assertRaises(Exception):
            UpstreamProxy.parse("socks5://127.0.0.1")


if __name__ == "__main__":
    unittest.main()

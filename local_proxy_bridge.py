"""Expose an authenticated proxy as a local browser-friendly HTTP proxy.

Camoufox and Playwright cannot reliably consume an authenticated SOCKS5 URL.
This module starts a loopback-only HTTP CONNECT proxy in a background thread
and forwards every tunnel through the selected upstream.  The browser therefore
uses one unauthenticated local endpoint while all traffic still leaves through
the original upstream exit IP.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import ssl
import threading
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

_SOCKS5_VERSION = 0x05
_SOCKS5_NO_AUTH = 0x00
_SOCKS5_USER_PASS = 0x02
_SOCKS5_CONNECT = 0x01
_SOCKS5_ATYP_IPV4 = 0x01
_SOCKS5_ATYP_DOMAIN = 0x03
_SOCKS5_ATYP_IPV6 = 0x04
_MAX_HEADER_BYTES = 64 * 1024


class ProxyBridgeError(RuntimeError):
    """Raised when the selected upstream proxy cannot be bridged."""


@dataclass(frozen=True)
class UpstreamProxy:
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = field(default="", repr=False)

    @classmethod
    def parse(cls, value: str) -> "UpstreamProxy":
        raw = (value or "").strip()
        if not raw:
            raise ProxyBridgeError("代理地址为空")
        if "://" not in raw:
            raw = f"http://{raw}"
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https", "socks5", "socks5h"}:
            raise ProxyBridgeError(f"不支持的代理协议: {scheme or 'unknown'}")
        try:
            host = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ProxyBridgeError(f"代理地址格式错误: {raw[:100]}") from exc
        if not host or not port:
            raise ProxyBridgeError(f"代理缺少主机或端口: {raw[:100]}")
        return cls(
            scheme=scheme,
            host=host,
            port=port,
            username=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
        )

    @property
    def masked(self) -> str:
        auth = "***@" if self.username else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"


@dataclass
class LocalProxyBridge:
    upstream: UpstreamProxy
    listen_host: str = "127.0.0.1"
    listen_port: int = 0
    connect_timeout: float = 15.0

    _server: asyncio.Server | None = field(default=None, init=False, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _port: int = field(default=0, init=False, repr=False)

    @property
    def local_url(self) -> str:
        if not self._port:
            raise ProxyBridgeError("本地代理桥尚未启动")
        return f"http://{self.listen_host}:{self._port}"

    def start(self) -> int:
        if self._server is not None:
            return self._port
        ready = threading.Event()
        failure: list[BaseException] = []

        def serve() -> None:
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                self._loop = loop
                server = loop.run_until_complete(
                    asyncio.start_server(
                        self._handle_client,
                        self.listen_host,
                        self.listen_port,
                        limit=_MAX_HEADER_BYTES,
                    )
                )
                self._server = server
                self._port = int(server.sockets[0].getsockname()[1])
                logger.info(
                    "本地代理桥已启动: %s -> %s",
                    self.local_url,
                    self.upstream.masked,
                )
                ready.set()
                loop.run_forever()
            except BaseException as exc:  # pragma: no cover - surfaced to caller
                failure.append(exc)
                ready.set()
            finally:
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()
                self._loop = None
                self._server = None
                self._port = 0

        self._thread = threading.Thread(target=serve, name="nexusflow-proxy-bridge", daemon=True)
        self._thread.start()
        if not ready.wait(timeout=15):
            raise ProxyBridgeError("本地代理桥启动超时")
        if failure:
            raise ProxyBridgeError(f"本地代理桥启动失败: {failure[0]}") from failure[0]
        if not self._port:
            raise ProxyBridgeError("本地代理桥未绑定端口")
        return self._port

    def stop(self) -> None:
        loop = self._loop
        server = self._server
        if loop is not None and loop.is_running():
            async def close_server() -> None:
                if server is not None:
                    server.close()
                    await server.wait_closed()

            try:
                asyncio.run_coroutine_threadsafe(close_server(), loop).result(timeout=3)
            except Exception:
                pass
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._loop = None
        self._server = None
        self._port = 0
        logger.info("本地代理桥已停止")

    def __enter__(self) -> "LocalProxyBridge":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        upstream_reader: asyncio.StreamReader | None = None
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            request_line, _headers = await asyncio.wait_for(
                self._read_http_head(reader),
                timeout=self.connect_timeout,
            )
            parts = request_line.split()
            if len(parts) < 3 or parts[0].upper() != "CONNECT":
                writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            host, port = self._parse_authority(parts[1])
            upstream_reader, upstream_writer = await asyncio.wait_for(
                self._connect_upstream(host, port),
                timeout=self.connect_timeout,
            )
            writer.write(b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: NexusFlow\r\n\r\n")
            await writer.drain()
            await self._relay(reader, writer, upstream_reader, upstream_writer)
        except Exception as exc:
            logger.debug("本地代理桥连接失败: %s: %s", type(exc).__name__, exc)
            try:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                await writer.drain()
            except Exception:
                pass
        finally:
            if upstream_writer is not None:
                try:
                    upstream_writer.close()
                    await upstream_writer.wait_closed()
                except Exception:
                    pass
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _connect_upstream(
        self,
        host: str,
        port: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self.upstream.scheme in {"http", "https"}:
            return await self._connect_http_proxy(host, port)
        return await self._connect_socks5_proxy(host, port)

    async def _connect_http_proxy(
        self,
        host: str,
        port: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        ssl_context = None
        server_hostname = None
        if self.upstream.scheme == "https":
            ssl_context = ssl.create_default_context()
            server_hostname = self.upstream.host
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                self.upstream.host,
                self.upstream.port,
                ssl=ssl_context,
                server_hostname=server_hostname,
            ),
            timeout=self.connect_timeout,
        )
        authority = self._format_authority(host, port)
        lines = [
            f"CONNECT {authority} HTTP/1.1",
            f"Host: {authority}",
            "Proxy-Connection: keep-alive",
        ]
        if self.upstream.username:
            credentials = f"{self.upstream.username}:{self.upstream.password}".encode("utf-8")
            token = base64.b64encode(credentials).decode("ascii")
            lines.append(f"Proxy-Authorization: Basic {token}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii", errors="replace"))
        await asyncio.wait_for(writer.drain(), timeout=self.connect_timeout)
        response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=self.connect_timeout)
        first_line = response.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
        pieces = first_line.split()
        if len(pieces) < 2 or not pieces[1].isdigit() or not 200 <= int(pieces[1]) < 300:
            writer.close()
            await writer.wait_closed()
            raise ProxyBridgeError(f"上游 HTTP 代理 CONNECT 失败: {first_line[:160]}")
        return reader, writer

    async def _connect_socks5_proxy(
        self,
        host: str,
        port: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.upstream.host, self.upstream.port),
            timeout=self.connect_timeout,
        )
        try:
            if self.upstream.username:
                writer.write(bytes([_SOCKS5_VERSION, 1, _SOCKS5_USER_PASS]))
            else:
                writer.write(bytes([_SOCKS5_VERSION, 1, _SOCKS5_NO_AUTH]))
            await asyncio.wait_for(writer.drain(), timeout=self.connect_timeout)
            method_reply = await asyncio.wait_for(reader.readexactly(2), timeout=self.connect_timeout)
            if method_reply[0] != _SOCKS5_VERSION or method_reply[1] == 0xFF:
                raise ProxyBridgeError("上游 SOCKS5 代理拒绝认证方式")
            if method_reply[1] == _SOCKS5_USER_PASS:
                username = self.upstream.username.encode("utf-8")
                password = self.upstream.password.encode("utf-8")
                if len(username) > 255 or len(password) > 255:
                    raise ProxyBridgeError("SOCKS5 用户名或密码过长")
                auth = (
                    bytes([0x01, len(username)])
                    + username
                    + bytes([len(password)])
                    + password
                )
                writer.write(auth)
                await asyncio.wait_for(writer.drain(), timeout=self.connect_timeout)
                auth_reply = await asyncio.wait_for(
                    reader.readexactly(2),
                    timeout=self.connect_timeout,
                )
                if auth_reply[1] != 0:
                    raise ProxyBridgeError("上游 SOCKS5 代理认证失败")

            address = self._encode_socks5_address(host)
            writer.write(
                bytes([_SOCKS5_VERSION, _SOCKS5_CONNECT, 0x00])
                + address
                + port.to_bytes(2, "big")
            )
            await asyncio.wait_for(writer.drain(), timeout=self.connect_timeout)
            reply_head = await asyncio.wait_for(reader.readexactly(4), timeout=self.connect_timeout)
            if reply_head[0] != _SOCKS5_VERSION or reply_head[1] != 0:
                raise ProxyBridgeError(f"上游 SOCKS5 CONNECT 失败: reply={reply_head[1]}")
            atyp = reply_head[3]
            if atyp == _SOCKS5_ATYP_IPV4:
                await asyncio.wait_for(reader.readexactly(4 + 2), timeout=self.connect_timeout)
            elif atyp == _SOCKS5_ATYP_IPV6:
                await asyncio.wait_for(reader.readexactly(16 + 2), timeout=self.connect_timeout)
            elif atyp == _SOCKS5_ATYP_DOMAIN:
                length = (await asyncio.wait_for(reader.readexactly(1), timeout=self.connect_timeout))[0]
                await asyncio.wait_for(reader.readexactly(length + 2), timeout=self.connect_timeout)
            else:
                raise ProxyBridgeError(f"上游 SOCKS5 返回未知地址类型: {atyp}")
            return reader, writer
        except Exception:
            writer.close()
            await writer.wait_closed()
            raise

    async def _read_http_head(
        self,
        reader: asyncio.StreamReader,
    ) -> tuple[str, dict[str, str]]:
        raw = await reader.readuntil(b"\r\n\r\n")
        if len(raw) > _MAX_HEADER_BYTES:
            raise ProxyBridgeError("代理请求头过大")
        lines = raw.decode("iso-8859-1", errors="replace").split("\r\n")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line or ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
        return lines[0], headers

    async def _relay(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        async def pipe(
            source: asyncio.StreamReader,
            destination: asyncio.StreamWriter,
        ) -> None:
            try:
                while True:
                    data = await source.read(65536)
                    if not data:
                        break
                    destination.write(data)
                    await destination.drain()
            except (ConnectionError, OSError, asyncio.CancelledError):
                pass
            finally:
                try:
                    if destination.can_write_eof():
                        destination.write_eof()
                except Exception:
                    pass

        tasks = [
            asyncio.create_task(pipe(client_reader, upstream_writer)),
            asyncio.create_task(pipe(upstream_reader, client_writer)),
        ]
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    @staticmethod
    def _parse_authority(value: str) -> tuple[str, int]:
        raw = (value or "").strip()
        if raw.startswith("["):
            closing = raw.find("]")
            if closing < 0 or not raw[closing + 1 :].startswith(":"):
                raise ProxyBridgeError(f"无效的 CONNECT 目标: {raw[:120]}")
            host = raw[1:closing]
            port_text = raw[closing + 2 :]
        else:
            host, separator, port_text = raw.rpartition(":")
            if not separator:
                raise ProxyBridgeError(f"CONNECT 目标缺少端口: {raw[:120]}")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ProxyBridgeError(f"CONNECT 目标端口无效: {raw[:120]}") from exc
        if not host or not 1 <= port <= 65535:
            raise ProxyBridgeError(f"CONNECT 目标无效: {raw[:120]}")
        return host, port

    @staticmethod
    def _format_authority(host: str, port: int) -> str:
        try:
            if ipaddress.ip_address(host).version == 6:
                return f"[{host}]:{port}"
        except ValueError:
            pass
        return f"{host}:{port}"

    @staticmethod
    def _encode_socks5_address(host: str) -> bytes:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            encoded = host.encode("idna")
            if not encoded or len(encoded) > 255:
                raise ProxyBridgeError("SOCKS5 目标域名无效")
            return bytes([_SOCKS5_ATYP_DOMAIN, len(encoded)]) + encoded
        if address.version == 4:
            return bytes([_SOCKS5_ATYP_IPV4]) + address.packed
        return bytes([_SOCKS5_ATYP_IPV6]) + address.packed


def proxy_for_browser(proxy: str) -> tuple[str, Callable[[], None]]:
    """Return a local browser proxy URL and an idempotent cleanup callback."""
    value = (proxy or "").strip()
    if not value:
        return "", _noop
    bridge = LocalProxyBridge(UpstreamProxy.parse(value))
    bridge.start()
    stopped = False

    def close() -> None:
        nonlocal stopped
        if not stopped:
            stopped = True
            bridge.stop()

    return bridge.local_url, close


def _noop() -> None:
    return None

"""基于动态 DataChannel 的 TCP 端口映射挂载。"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable

from ..webrtc import TunnelChannel, TunnelSession, TunnelSessionError


_FORWARD_ID = re.compile(r"[0-9a-f]{8}\Z")
_HEADER_VERSION = 1
_CONNECT_TIMEOUT_S = 5.0
_HEADER_TIMEOUT_S = 5.0
_STREAM_CHUNK_BYTES = 16_384
_MAX_ACTIVE_CONNECTIONS = 128


@dataclass(frozen=True, slots=True, kw_only=True)
class ForwardSpec:
    """一个控制端 TCP listener 到被控端目标的稳定配置。"""

    forward_id: str
    peer_key: str
    listen_port: int
    target_host: str
    target_port: int
    listen_host: str = "127.0.0.1"

    def __post_init__(self) -> None:
        if not _FORWARD_ID.fullmatch(self.forward_id):
            raise ValueError("forward_id must be 8 lowercase hex characters")
        if not self.peer_key or self.peer_key != self.peer_key.strip():
            raise ValueError("peer_key is invalid")
        _validate_host(self.listen_host, "listen_host")
        _validate_host(self.target_host, "target_host")
        if type(self.listen_port) is not int or not 0 <= self.listen_port <= 65535:
            raise ValueError("listen_port is invalid")
        _validate_port(self.target_port, "target_port")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def encode_forward_header(host: str, port: int) -> str:
    """编码动态通道首帧。"""

    _validate_host(host, "host")
    _validate_port(port, "port")
    return json.dumps(
        {"v": _HEADER_VERSION, "host": host, "port": port},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_forward_header(text: str) -> tuple[str, int]:
    """严格校验动态通道首帧。"""

    if not isinstance(text, str):
        raise ValueError("forward header must be text")
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("forward header is invalid JSON") from exc
    if not isinstance(value, dict) or set(value) != {"v", "host", "port"}:
        raise ValueError("forward header fields are invalid")
    if value.get("v") != _HEADER_VERSION:
        raise ValueError("forward header version is unsupported")
    host = value.get("host")
    port = value.get("port")
    _validate_host(host, "host")
    _validate_port(port, "port")
    return host, port


class ForwardListener:
    """控制端 TCP listener；每个入站连接使用一个动态通道。"""

    def __init__(self, spec: ForwardSpec, session: TunnelSession) -> None:
        self.spec = spec
        self._session = session
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[asyncio.Task[None]] = set()
        self._lifecycle_lock = asyncio.Lock()
        self._closed = False

    @property
    def session(self) -> TunnelSession:
        return self._session

    def rebind_session(self, session: TunnelSession) -> None:
        """切换后续入站连接使用的 transport；既有连接不迁移。"""

        self._session = session

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise TunnelSessionError("端口映射已关闭")
            self._server = await asyncio.start_server(
                self._accept,
                self.spec.listen_host,
                self.spec.listen_port,
            )
            if self.spec.listen_port == 0:
                port = self._server.sockets[0].getsockname()[1]
                self.spec = replace(self.spec, listen_port=port)

    async def close(self) -> None:
        async with self._lifecycle_lock:
            self._closed = True
            server = self._server
            self._server = None
            if server is not None:
                server.close()
        tasks = tuple(self._connections)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            await server.wait_closed()

    def _accept(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if self._closed or len(self._connections) >= _MAX_ACTIVE_CONNECTIONS:
            writer.close()
            return
        task = asyncio.create_task(
            self._handle(reader, writer),
            name=f"uu-tunnel-forward-{self.spec.forward_id}",
        )
        self._connections.add(task)
        task.add_done_callback(self._connections.discard)
        task.add_done_callback(_consume_task)
        task.add_done_callback(lambda _task: writer.close())

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        channel: TunnelChannel | None = None
        try:
            channel = await self._session.create_channel(
                f"FWD/{secrets.token_hex(4)}"
            )
            channel.send_text(
                encode_forward_header(self.spec.target_host, self.spec.target_port)
            )
            await _relay(reader, writer, channel)
        finally:
            if channel is not None:
                channel.close()
            await _close_writer(writer)


class ForwardAcceptor:
    """被控端动态通道接收器。

    目标 host 来自可信隧道对端，当前不设白名单；后续策略钩子应位于
    ``validate_target`` 回调处。
    """

    def __init__(
        self,
        *,
        validate_target: Callable[[str, int], bool] | None = None,
        fixed_destination: tuple[str, int] | None = None,
    ) -> None:
        self._validate_target = validate_target or (lambda _host, _port: True)
        self._fixed_destination = fixed_destination

    async def accept(self, channel: TunnelChannel) -> None:
        writer: asyncio.StreamWriter | None = None
        try:
            first = await asyncio.wait_for(
                channel.recv(),
                timeout=_HEADER_TIMEOUT_S,
            )
            host, port = decode_forward_header(first)  # type: ignore[arg-type]
            if not self._validate_target(host, port):
                raise ValueError("forward target was rejected by policy")
            if self._fixed_destination is not None:
                host, port = self._fixed_destination
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=_CONNECT_TIMEOUT_S,
            )
            await _relay(reader, writer, channel)
        finally:
            channel.close()
            if writer is not None:
                await _close_writer(writer)


async def _relay(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    channel: TunnelChannel,
) -> None:
    to_channel = asyncio.create_task(_socket_to_channel(reader, channel))
    to_socket = asyncio.create_task(_channel_to_socket(channel, writer))
    tasks = (to_channel, to_socket)
    pending: set[asyncio.Task[None]] = set(tasks)
    try:
        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_EXCEPTION,
        )
        for task in done:
            await task
    finally:
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _socket_to_channel(
    reader: asyncio.StreamReader,
    channel: TunnelChannel,
) -> None:
    while data := await reader.read(_STREAM_CHUNK_BYTES):
        await channel.send_bytes(data)
    await channel.send_bytes(b"")


async def _channel_to_socket(
    channel: TunnelChannel,
    writer: asyncio.StreamWriter,
) -> None:
    while True:
        data = await channel.recv()
        if not isinstance(data, bytes):
            raise ValueError("forward stream received unexpected text")
        if not data:
            if not writer.can_write_eof():
                raise RuntimeError("target socket does not support half-close")
            writer.write_eof()
            await writer.drain()
            return
        writer.write(data)
        await writer.drain()


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
    except (asyncio.TimeoutError, OSError, ConnectionError):
        return


def _validate_host(value: Any, field: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 253
    ):
        raise ValueError(f"{field} is invalid")


def _validate_port(value: Any, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError(f"{field} is invalid")


def _consume_task(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        return


__all__ = [
    "ForwardAcceptor",
    "ForwardListener",
    "ForwardSpec",
    "decode_forward_header",
    "encode_forward_header",
]

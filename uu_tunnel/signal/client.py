"""Socket.IO-backed client for UU's signalling service.

The application only needs a small signal-facing API, while
``python-socketio`` owns the Engine.IO v4 / Socket.IO v5 framing, heartbeat,
binary attachment reconstruction, and acknowledgement packet handling.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import engineio
import socketio
from engineio import packet as engineio_packet

logger = logging.getLogger("uu-tunnel.signal")

_NAMESPACE = "/"
_SOCKETIO_PATH = "socket.io"
_UU_BINARY_MESSAGE_PREFIX = b"\x04"
_UNKNOWN_SERVER_LABEL = "未知信令服务器"


class SignalConnectionError(ConnectionError):
    """A credential-safe Engine.IO or Socket.IO transport operation failed."""


class _UuEngineIoAsyncClient(engineio.AsyncClient):
    """Apply UU's binary MESSAGE framing at Engine.IO's packet boundaries.

    UU's WebSocket signal endpoints retain a leading ``0x04`` on binary
    Engine.IO MESSAGE frames. Standard Engine.IO v4 clients send and receive
    those payloads raw, so this narrow adapter restores only that dialect
    detail while leaving Socket.IO packet/attachment handling library-owned.
    """

    async def _send_packet(self, pkt: engineio_packet.Packet) -> None:
        await super()._send_packet(_prefix_uu_binary_message(pkt))

    async def _receive_packet(self, pkt: engineio_packet.Packet) -> None:
        await super()._receive_packet(_strip_uu_binary_message_prefix(pkt))


class _UuSocketIoAsyncClient(socketio.AsyncClient):
    """Socket.IO client wired to the UU-specific Engine.IO adapter."""

    def _engineio_client_class(self) -> type[engineio.AsyncClient]:
        return _UuEngineIoAsyncClient


@dataclass
class SignalCallbacks:
    """Connection lifecycle callbacks supplied by :class:`TunnelSession`."""

    on_event: Callable[[str, list[Any]], None]  # (event, args)
    on_close: Callable[[str], None]
    on_error: Callable[[str], None]


def normalize_signal_server(signal_server: str) -> str:
    """Return an HTTP(S) base URL suitable for ``AsyncClient.connect``.

    Room responses have historically supplied bare hosts, while
    ``python-socketio`` expects an HTTP(S) URL and creates the WebSocket URL
    itself. Bare addresses retain the secure-default behaviour. ``wss`` is
    accepted for room configurations that describe the resulting transport,
    then mapped to the HTTPS base URL expected by python-socketio.
    """
    if not isinstance(signal_server, str):
        raise ValueError("无效的信令服务器地址")
    if _contains_control_characters(signal_server):
        raise ValueError("信令服务器地址不能包含控制字符")

    raw = signal_server.strip()
    if not raw:
        raise ValueError("信令服务器地址不能为空")
    if any(char.isspace() for char in raw):
        raise ValueError("信令服务器地址不能包含空白字符")
    # Fragments never belong to a server request and are especially easy to
    # accidentally use for local credentials. Reject rather than silently
    # dropping one.
    if "#" in raw:
        raise ValueError("信令服务器地址不能包含片段")

    raw = _with_secure_default_scheme(raw)
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        # Accessing ``port`` validates malformed and out-of-range ports without
        # ever including the untrusted URL in an exception message.
        parsed.port
    except ValueError:
        raise ValueError("无效的信令服务器地址") from None

    scheme = parsed.scheme.lower()
    if scheme in {"http", "ws"}:
        raise _signal_url_error("信令服务器必须使用 HTTPS 或 WSS", hostname)
    if scheme not in {"https", "wss"}:
        raise _signal_url_error("不支持的信令服务器协议", hostname)
    if not hostname:
        raise ValueError("无效的信令服务器地址")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise _signal_url_error("信令服务器地址不能包含用户名或密码", hostname)
    if parsed.fragment:
        # Keep this defensive check in case a future URL preprocessor changes
        # the raw-fragment validation above.
        raise _signal_url_error("信令服务器地址不能包含片段", hostname)

    return urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/"), parsed.query, ""))


class SocketIoSignalClient:
    """A single UU signalling connection backed by ``socketio.AsyncClient``."""

    def __init__(self, callbacks: SignalCallbacks):
        self._callbacks = callbacks
        self._sio: socketio.AsyncClient | None = None
        self._server_label = _UNKNOWN_SERVER_LABEL
        self._closed = False
        self._connecting = False
        self._close_notified = False
        self._error_notified = False
        self._emit_lock = asyncio.Lock()
        self._pending_acks: set[asyncio.Future[list[Any]]] = set()

    @property
    def connected(self) -> bool:
        """Whether the default Socket.IO namespace is currently available."""
        return bool(
            self._sio is not None
            and not self._closed
            and getattr(self._sio, "connected", False)
        )

    async def connect(
        self, signal_server: str, headers: dict[str, str], timeout: float = 10.0
    ) -> None:
        """Connect directly over WebSocket and wait for namespace ``/``.

        Reconnection is intentionally disabled. ``TunnelSession`` owns fresh
        room/session retries and host failover; transport-level reconnects can
        replay control or SOAC messages into a stale room attempt.
        """
        if self._sio is not None or self._connecting:
            raise RuntimeError("信令客户端已连接")

        self._closed = False
        self._close_notified = False
        self._error_notified = False
        self._server_label = _UNKNOWN_SERVER_LABEL
        try:
            url = normalize_signal_server(signal_server)
            connect_url, socketio_path = _socketio_target(url)
            self._server_label = _signal_server_label(url)
        except ValueError as exc:
            # A bad room-config host is an initial connection failure, not a
            # session-ending disconnect. TunnelSession can advance to the next
            # advertised host just as it does for a failed dial.
            reason = f"信令连接失败: {exc}"
            self._closed = True
            self._sio = None
            self._connecting = False
            self._notify_error_once(reason)
            raise SignalConnectionError(reason) from None
        client_options: dict[str, Any] = {
            # TunnelSession deliberately creates a fresh room attempt after a
            # failure. Never let Socket.IO reconnect and replay control/SOAC.
            "reconnection": False,
            "handle_sigint": False,
            "request_timeout": timeout,
            # Use the library's normal verified TLS context for DNS and IP
            # literals alike. An IP certificate mismatch must fail this host
            # so TunnelSession can try the next advertised server.
            "ssl_verify": True,
        }

        sio = _UuSocketIoAsyncClient(**client_options)
        self._sio = sio
        self._connecting = True
        self._register_handlers(sio)

        logger.debug("连接信令服务器 %s", self._server_label)
        try:
            await asyncio.wait_for(
                sio.connect(
                    connect_url,
                    headers=dict(headers),
                    transports=["websocket"],
                    namespaces=[_NAMESPACE],
                    socketio_path=socketio_path,
                    wait=True,
                    wait_timeout=timeout,
                    retry=False,
                ),
                timeout=timeout,
            )
            if (
                self._closed
                or self._sio is not sio
                or not getattr(sio, "connected", False)
            ):
                await self._disconnect_quietly(sio)
                raise RuntimeError("信令客户端在连接期间已关闭")
        except asyncio.CancelledError:
            await self._abort_connect(sio)
            raise
        except Exception:
            # Socket.IO/aiohttp exceptions can echo the request URL or headers.
            # Surface only the validated hostname and let TunnelSession fail
            # over to the next room-advertised signal host.
            reason = self._signal_failure_reason("信令连接失败")
            await self._abort_connect(sio, reason)
            raise SignalConnectionError(reason) from None
        finally:
            self._connecting = False

    async def close(self) -> None:
        """Actively close the connection without reporting a transport error."""
        if self._closed and self._sio is None:
            return

        self._closed = True
        sio = self._sio
        self._sio = None
        self._fail_pending_acks(RuntimeError("信令客户端已关闭"))
        if sio is not None:
            await self._disconnect_quietly(sio)

    async def emit_with_ack(
        self, event: str, payload: dict[str, Any], ack_timeout: float = 10.0
    ) -> list[Any]:
        """Emit an event and return its Socket.IO acknowledgement arguments.

        ``AsyncClient`` reconstructs binary ACK attachments before it invokes
        the callback. Keeping the original callback argument boundaries gives
        the application the same ``list[Any]`` shape as the former wire
        decoder (including ``[]`` for an empty ACK).
        """
        self._assert_connected()
        sio = self._sio
        assert sio is not None
        result: asyncio.Future[list[Any]] = asyncio.get_running_loop().create_future()
        self._pending_acks.add(result)

        def acknowledge(*args: Any) -> None:
            # Socket.IO can deliver a queued ACK after disconnect(). Do not
            # revive a waiter belonging to a closed or replaced connection.
            if self._is_current_connection(sio) and not result.done():
                result.set_result(list(args))

        try:
            # A Socket.IO binary event is multiple packets. Serializing the
            # whole emit prevents attachments from concurrent emits interleaving.
            async with self._emit_lock:
                self._assert_current_connection(sio)
                await sio.emit(event, payload, namespace=_NAMESPACE, callback=acknowledge)
            return await asyncio.wait_for(result, timeout=ack_timeout)
        except asyncio.TimeoutError:
            self._finish_abandoned_ack(result)
            raise TimeoutError(f"信令事件回执超时（{ack_timeout}s）") from None
        except asyncio.CancelledError:
            self._finish_abandoned_ack(result)
            raise
        except Exception:
            self._finish_abandoned_ack(result)
            raise SignalConnectionError(self._signal_failure_reason("信令事件发送失败")) from None
        finally:
            self._pending_acks.discard(result)

    async def emit_no_ack(self, event: str, payload: dict[str, Any]) -> None:
        """Emit an event without waiting for a Socket.IO acknowledgement."""
        try:
            async with self._emit_lock:
                self._assert_connected()
                sio = self._sio
                assert sio is not None
                await sio.emit(event, payload, namespace=_NAMESPACE)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise SignalConnectionError(self._signal_failure_reason("信令事件发送失败")) from None

    def _register_handlers(self, sio: socketio.AsyncClient) -> None:
        def on_disconnect(*args: Any) -> None:
            if self._is_current_connection(sio):
                self._on_disconnect(*args)

        def on_connect_error(*args: Any) -> None:
            if self._is_current_connection(sio):
                self._on_connect_error(*args)

        def on_event(event: str, *args: Any) -> None:
            if self._is_current_connection(sio):
                self._on_event(event, *args)

        sio.on("disconnect", on_disconnect, namespace=_NAMESPACE)
        sio.on("connect_error", on_connect_error, namespace=_NAMESPACE)
        # The catch-all handler receives ``(event_name, *event_args)`` and
        # preserves binary values decoded by python-socketio.
        sio.on("*", on_event, namespace=_NAMESPACE)

    def _on_event(self, event: str, *args: Any) -> None:
        if self._closed:
            return
        try:
            self._callbacks.on_event(event, list(args))
        except Exception:  # noqa: BLE001
            # Callback exceptions can embed the event payload. Keep the log
            # useful without serializing the event, arguments, or traceback.
            logger.error("信令事件回调失败")

    def _on_connect_error(self, *args: Any) -> None:
        if not self._closed:
            self._notify_error_once(
                _callback_reason("Socket.IO 连接错误", args, self._server_label)
            )

    def _on_disconnect(self, *args: Any) -> None:
        # AsyncClient invokes this handler for an active ``disconnect()`` as
        # well. The flag is set before that call, so an intentional close does
        # not masquerade as a signal-server failure.
        if self._closed:
            return
        self._closed = True
        self._sio = None
        reason = _callback_reason("Socket.IO 连接断开", args, self._server_label)
        self._fail_pending_acks(RuntimeError(f"信令连接断开: {reason}"))
        self._notify_close_once(reason)

    def _assert_connected(self) -> None:
        if not self.connected:
            raise RuntimeError("信令客户端未连接")

    def _is_current_connection(self, sio: socketio.AsyncClient) -> bool:
        """Whether a callback still belongs to this live Socket.IO client."""
        return not self._closed and self._sio is sio

    def _assert_current_connection(self, sio: socketio.AsyncClient) -> None:
        if not self._is_current_connection(sio) or not getattr(sio, "connected", False):
            raise RuntimeError("信令客户端未连接")

    def _signal_failure_reason(self, prefix: str) -> str:
        return f"{prefix}（{self._server_label}）"

    def _fail_pending_acks(self, error: Exception) -> None:
        for future in tuple(self._pending_acks):
            if not future.done():
                future.set_exception(error)

    @staticmethod
    def _finish_abandoned_ack(future: asyncio.Future[list[Any]]) -> None:
        """Cancel an ACK waiter that exits before it can consume its result."""
        if not future.done():
            future.cancel()
        elif not future.cancelled():
            # If disconnect won the race while the caller was still waiting
            # for the emit lock, the caller sees a different exception below.
            # Mark the ACK failure retrieved so it cannot become unobserved.
            future.exception()

    def _notify_close_once(self, reason: str) -> None:
        if self._close_notified:
            return
        self._close_notified = True
        try:
            self._callbacks.on_close(reason)
        except Exception:  # noqa: BLE001
            logger.error("信令关闭回调失败")

    def _notify_error_once(self, reason: str) -> None:
        if self._error_notified:
            return
        self._error_notified = True
        try:
            self._callbacks.on_error(reason)
        except Exception:  # noqa: BLE001
            logger.error("信令错误回调失败")

    @staticmethod
    async def _disconnect_quietly(sio: socketio.AsyncClient) -> None:
        try:
            await sio.disconnect()
        except Exception:  # noqa: BLE001
            # A failed handshake can leave Engine.IO half-initialised. It is
            # already being surfaced by connect(), so cleanup must not hide it.
            logger.debug("关闭信令客户端时忽略传输异常")

    async def _abort_connect(
        self, sio: socketio.AsyncClient, error_reason: str | None = None
    ) -> None:
        """Clean up a failed initial connection without emitting a close event."""
        if error_reason is not None and not self._closed:
            self._notify_error_once(error_reason)
        self._closed = True
        self._fail_pending_acks(RuntimeError("信令客户端已关闭"))
        await self._disconnect_quietly(sio)
        if self._sio is sio:
            self._sio = None


def _socketio_target(url: str) -> tuple[str, str]:
    """Split an optional server path into AsyncClient URL and socket.io path."""
    parsed = urlsplit(url)
    prefix = parsed.path.strip("/")
    # A room response can already include the Socket.IO endpoint. Preserve it
    # instead of producing ``.../socket.io/socket.io``.
    if prefix == _SOCKETIO_PATH or prefix.endswith(f"/{_SOCKETIO_PATH}"):
        socketio_path = prefix
    else:
        socketio_path = f"{prefix}/{_SOCKETIO_PATH}" if prefix else _SOCKETIO_PATH
    connect_url = urlunsplit((parsed.scheme, parsed.netloc, "", parsed.query, ""))
    return connect_url, socketio_path


def _is_binary_message(pkt: engineio_packet.Packet) -> bool:
    """Return whether *pkt* is a binary Engine.IO MESSAGE packet."""
    return (
        pkt.packet_type == engineio_packet.MESSAGE
        and pkt.binary
        and isinstance(pkt.data, (bytes, bytearray))
    )


def _prefix_uu_binary_message(pkt: engineio_packet.Packet) -> engineio_packet.Packet:
    """Return a queue-safe binary MESSAGE packet with UU's one-byte prefix.

    The original packet is never mutated, including its cached encoding. This
    keeps a retry or repeated observation of the same packet from accumulating
    prefixes.
    """
    if not _is_binary_message(pkt) or getattr(pkt, "_uu_binary_prefix_added", False):
        return pkt
    prefixed = engineio_packet.Packet(
        engineio_packet.MESSAGE,
        data=_UU_BINARY_MESSAGE_PREFIX + bytes(pkt.data),
    )
    # This marker belongs only to the fresh queue packet. It makes a transport
    # retry of that same queued object idempotent without confusing a raw
    # application attachment whose first byte is legitimately 0x04.
    prefixed._uu_binary_prefix_added = True
    return prefixed


def _strip_uu_binary_message_prefix(pkt: engineio_packet.Packet) -> engineio_packet.Packet:
    """Remove exactly one UU prefix from an incoming binary MESSAGE packet."""
    if not _is_binary_message(pkt):
        return pkt
    data = bytes(pkt.data)
    if not data.startswith(_UU_BINARY_MESSAGE_PREFIX):
        return pkt
    return engineio_packet.Packet(engineio_packet.MESSAGE, data=data[1:])


def _contains_control_characters(value: str) -> bool:
    """Return whether *value* contains a C0 or C1 control character."""
    return any(ord(char) <= 0x1F or 0x7F <= ord(char) <= 0x9F for char in value)


def _with_secure_default_scheme(raw: str) -> str:
    """Add HTTPS to a bare authority without confusing an IPv6 literal for a scheme."""
    if raw.startswith("//"):
        return "https:" + raw
    if "://" in raw:
        return raw
    return "https://" + _bracket_bare_ipv6_literal(raw)


def _bracket_bare_ipv6_literal(raw: str) -> str:
    """Bracket a bare IPv6 authority while preserving its path and query."""
    if raw.startswith("["):
        return raw

    authority_end = len(raw)
    for delimiter in ("/", "?"):
        position = raw.find(delimiter)
        if position >= 0:
            authority_end = min(authority_end, position)
    authority = raw[:authority_end]
    if ":" in authority and _is_ip_address(authority):
        return f"[{authority}]{raw[authority_end:]}"
    return raw


def _signal_url_error(message: str, hostname: str | None) -> ValueError:
    """Build a validation error without echoing an untrusted URL or credentials."""
    return ValueError(f"{message}（{_hostname_label(hostname)}）")


def _signal_server_label(url: str) -> str:
    """Return only a hostname suitable for diagnostic messages."""
    try:
        hostname = urlsplit(url).hostname
    except ValueError:
        return _UNKNOWN_SERVER_LABEL
    return _hostname_label(hostname)


def _hostname_label(hostname: str | None) -> str:
    if not hostname:
        return _UNKNOWN_SERVER_LABEL
    # Brackets make an IPv6 literal unambiguous while omitting its port, path,
    # query, userinfo, and any room token.
    return f"[{hostname}]" if ":" in hostname else hostname


def _is_ip_address(host: str) -> bool:
    """Return whether *host* is an IPv4 or IPv6 literal."""
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _callback_reason(
    prefix: str, _args: tuple[Any, ...], server_label: str = _UNKNOWN_SERVER_LABEL
) -> str:
    """Return a lifecycle reason without inspecting server-supplied payloads."""
    return f"{prefix}（{server_label}）"

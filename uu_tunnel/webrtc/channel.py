"""Session-owned, bounded access to one aiortc data channel."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from aiortc import RTCDataChannel


MAX_INCOMING_MESSAGES = 256
MAX_TEXT_BYTES = 1024
MIN_PACKET_BYTES = 20
MAX_PACKET_BYTES = 1280
MAX_STREAM_BYTES = 65_536
HIGH_WATER_BYTES = 524_288
LOW_WATER_BYTES = 131_072
# 终止时入队的哨兵：唤醒阻塞在 recv 的消费者，且保证先于它入队的消息仍按序交付。
_TERMINAL = object()


class TunnelChannelError(RuntimeError):
    """A terminal or invalid operation on a tunnel data channel."""


FailureCallback = Callable[[TunnelChannelError], None]
LifecycleCallback = Callable[[], None]


class TunnelChannel:
    """Bounded, session-owned wrapper around one :class:`RTCDataChannel`.

    The wrapper deliberately owns only its listeners and queues.  Closing the
    underlying aiortc channel remains the enclosing ``TunnelSession``'s job.
    """

    def __init__(
        self,
        raw: RTCDataChannel,
        failure_callback: FailureCallback,
        *,
        on_open: LifecycleCallback | None = None,
        on_close: LifecycleCallback | None = None,
        activate: bool = True,
        binary_min: int = MIN_PACKET_BYTES,
        binary_max: int = MAX_PACKET_BYTES,
    ) -> None:
        self._raw = raw
        self._failure_callback = failure_callback
        self._open_callback = on_open
        self._close_callback = on_close
        self._binary_min = binary_min
        self._binary_max = binary_max
        if not 0 <= binary_min <= binary_max <= MAX_STREAM_BYTES:
            raise ValueError("DataChannel binary limits are invalid")

        self._incoming: asyncio.Queue[object] = asyncio.Queue(
            maxsize=MAX_INCOMING_MESSAGES
        )
        self._terminal_event = asyncio.Event()
        self._low_event = asyncio.Event()
        self._send_lock = asyncio.Lock()

        self._terminal_error: TunnelChannelError | None = None
        self._bound = False
        self._detached = False
        self._active = False
        self._open_seen = False
        self._close_seen = False
        self._open_notified = False
        self._close_notified = False
        self._bound_listeners: list[tuple[str, Callable[..., None]]] = []

        # Keep the precise bound callback objects: pyee's remove_listener
        # requires the same object that was passed to on().
        self._message_listener = self._on_message
        self._open_listener = self._on_open
        self._close_listener = self._on_close
        self._bufferedamountlow_listener = self._on_bufferedamountlow

        self._bind_listeners()
        try:
            self._raw.bufferedAmountLowThreshold = LOW_WATER_BYTES
            if self._buffered_amount() <= LOW_WATER_BYTES:
                self._low_event.set()
        except Exception:  # noqa: BLE001
            self._fail(TunnelChannelError("DataChannel low-water setup failed"))

        if activate:
            self.activate()

    @property
    def raw(self) -> RTCDataChannel:
        """The underlying channel, retained for session-owned teardown only."""
        return self._raw

    @property
    def ready(self) -> bool:
        return (
            self._open_seen
            and not self._terminal_event.is_set()
            and not self._detached
            and self._ready_state() == "open"
        )

    def activate(self) -> None:
        """Enable lifecycle notifications after the owning session stores us."""
        if self._active:
            return
        self._active = True
        state = self._ready_state()
        if self._close_seen or state == "closed":
            self._notify_close()
        elif state == "open":
            self._on_open()

    async def recv(self) -> str | bytes:
        """Receive the next accepted message without hanging after termination."""
        try:
            message = self._incoming.get_nowait()
        except asyncio.QueueEmpty:
            self._raise_if_terminal_or_closed()
            message = await self._incoming.get()
        if message is _TERMINAL:
            # 哨兵只在终止后入队；队列满时未入队的场景由上面的空队列检查兜底。
            self._raise_if_terminal_or_closed()
            raise AssertionError("terminal DataChannel sentinel without terminal state")
        return message  # type: ignore[return-value]

    async def wait_ready(self, timeout: float | None = None) -> None:
        """等待动态通道打开或终止。"""

        if self.ready:
            return
        open_wait = asyncio.create_task(self._wait_until_ready())
        terminal_wait = asyncio.create_task(self._terminal_event.wait())
        try:
            done, _pending = await asyncio.wait(
                (open_wait, terminal_wait),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError("DataChannel open timed out")
            if terminal_wait in done:
                self._raise_if_terminal_or_closed()
            await open_wait
        finally:
            for waiter in (open_wait, terminal_wait):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(open_wait, terminal_wait, return_exceptions=True)

    async def _wait_until_ready(self) -> None:
        while not self.ready:
            self._raise_if_terminal_or_closed()
            await asyncio.sleep(0)

    def send_text(self, text: str) -> None:
        """Send one bounded UTF-8 text message while the channel is open."""
        if not isinstance(text, str):
            raise TypeError("DataChannel text must be str")
        try:
            encoded = text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("DataChannel text is not valid UTF-8") from exc
        if len(encoded) > MAX_TEXT_BYTES:
            raise ValueError(f"DataChannel text exceeds {MAX_TEXT_BYTES} UTF-8 bytes")

        self._ensure_open()
        try:
            self._raw.send(text)
        except Exception as exc:  # noqa: BLE001
            error = TunnelChannelError("DataChannel text send failed")
            self._fail(error)
            raise error from exc

    async def send_packet(self, packet: bytes) -> None:
        """Send one bounded packet, applying serialized outgoing backpressure."""
        if not isinstance(packet, bytes):
            raise TypeError("DataChannel packet must be bytes")
        if not MIN_PACKET_BYTES <= len(packet) <= MAX_PACKET_BYTES:
            raise ValueError(
                f"DataChannel packet must be {MIN_PACKET_BYTES}..{MAX_PACKET_BYTES} bytes"
            )

        await self._send_binary(packet)

    async def send_bytes(self, data: bytes) -> None:
        """发送动态字节流分片并应用 bufferedAmount 背压。"""

        if not isinstance(data, bytes):
            raise TypeError("DataChannel stream data must be bytes")
        if not self._binary_min <= len(data) <= self._binary_max:
            raise ValueError(
                f"DataChannel stream data must be {self._binary_min}..{self._binary_max} bytes"
            )
        await self._send_binary(data)

    async def _send_binary(self, data: bytes) -> None:
        async with self._send_lock:
            while True:
                self._ensure_open()
                try:
                    buffered = self._buffered_amount()
                except Exception as exc:  # noqa: BLE001
                    error = TunnelChannelError("DataChannel buffered amount is unavailable")
                    self._fail(error)
                    raise error from exc

                if buffered + len(data) <= HIGH_WATER_BYTES:
                    try:
                        self._raw.send(data)
                    except Exception as exc:  # noqa: BLE001
                        error = TunnelChannelError("DataChannel packet send failed")
                        self._fail(error)
                        raise error from exc
                    return

                await self._wait_for_low_or_terminal()

    def close(self) -> None:
        """关闭动态通道并唤醒本地 waiter。

        aiortc 的 close 事件在 stream reset 确认后才异步到达，而 detach 已移除
        监听器；因此这里同步通知 on_close，让会话立即释放该通道的登记。
        """

        try:
            self._raw.close()
        finally:
            self.detach()
            if self._active:
                self._notify_close()

    async def wait_closed(self) -> TunnelChannelError:
        """Wait for a close, detach, or terminal wrapper failure."""
        if self._ready_state() == "closed" and not self._terminal_event.is_set():
            self._on_close()
        await self._terminal_event.wait()
        return self._terminal_exception()

    def detach(self) -> None:
        """Remove only this wrapper's retained listeners and wake all waiters."""
        if self._detached:
            return
        self._detached = True
        if self._bound:
            remove_listener = getattr(self._raw, "remove_listener", None)
            if callable(remove_listener):
                for event, listener in self._bound_listeners:
                    try:
                        remove_listener(event, listener)
                    except Exception:  # noqa: BLE001
                        # Teardown must still wake consumers if a foreign or
                        # already-closed emitter rejects listener removal.
                        pass
            self._bound_listeners.clear()
            self._bound = False
        self._set_terminal(TunnelChannelError("DataChannel detached"))

    def _bind_listeners(self) -> None:
        try:
            for event, listener in self._listeners():
                self._raw.on(event, listener)
                self._bound_listeners.append((event, listener))
        except Exception:  # noqa: BLE001
            remove_listener = getattr(self._raw, "remove_listener", None)
            if callable(remove_listener):
                for event, listener in self._bound_listeners:
                    try:
                        remove_listener(event, listener)
                    except Exception:  # noqa: BLE001
                        pass
            self._bound_listeners.clear()
            self._bound = False
            self._fail(TunnelChannelError("DataChannel listener setup failed"))
            return
        self._bound = True

    def _listeners(self) -> tuple[tuple[str, Callable[..., None]], ...]:
        return (
            ("message", self._message_listener),
            ("open", self._open_listener),
            ("close", self._close_listener),
            ("bufferedamountlow", self._bufferedamountlow_listener),
        )

    def _on_message(self, message: Any) -> None:
        if self._terminal_event.is_set() or self._detached:
            return
        if isinstance(message, str):
            try:
                valid = len(message.encode("utf-8")) <= MAX_TEXT_BYTES
            except UnicodeEncodeError:
                valid = False
            if not valid:
                self._fail(TunnelChannelError("DataChannel received invalid text"))
                return
        elif isinstance(message, bytes):
            if not self._binary_min <= len(message) <= self._binary_max:
                self._fail(TunnelChannelError("DataChannel received invalid packet"))
                return
        else:
            self._fail(TunnelChannelError("DataChannel received an unsupported message type"))
            return
        try:
            self._incoming.put_nowait(message)
        except asyncio.QueueFull:
            self._fail(TunnelChannelError("DataChannel incoming queue overflow"))

    def _on_open(self, *_args: Any) -> None:
        if self._terminal_event.is_set() or self._detached:
            return
        self._open_seen = True
        self._low_event.set()
        if self._active and not self._open_notified:
            self._open_notified = True
            self._invoke_lifecycle_callback(self._open_callback)

    def _on_close(self, *_args: Any) -> None:
        self._close_seen = True
        self._set_terminal(TunnelChannelError("DataChannel closed"))
        if self._active:
            self._notify_close()

    def _on_bufferedamountlow(self, *_args: Any) -> None:
        self._low_event.set()

    def _notify_close(self) -> None:
        if not self._close_notified:
            self._close_notified = True
            self._invoke_lifecycle_callback(self._close_callback)

    async def _wait_for_low_or_terminal(self) -> None:
        self._low_event.clear()
        self._ensure_open()
        if self._buffered_amount() <= LOW_WATER_BYTES:
            return

        low_wait = asyncio.create_task(self._low_event.wait())
        terminal_wait = asyncio.create_task(self._terminal_event.wait())
        try:
            await asyncio.wait(
                {low_wait, terminal_wait}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for waiter in (low_wait, terminal_wait):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(low_wait, terminal_wait, return_exceptions=True)
        self._ensure_open()

    def _ensure_open(self) -> None:
        self._raise_if_terminal_or_closed()
        if not self.ready:
            raise TunnelChannelError("DataChannel is not open")

    def _raise_if_terminal_or_closed(self) -> None:
        if self._ready_state() == "closed" and not self._terminal_event.is_set():
            self._on_close()
        if self._terminal_event.is_set() or self._detached:
            raise self._terminal_exception()

    def _set_terminal(self, error: TunnelChannelError) -> bool:
        if self._terminal_event.is_set():
            return False
        self._terminal_error = error
        self._terminal_event.set()
        self._low_event.set()
        try:
            self._incoming.put_nowait(_TERMINAL)
        except asyncio.QueueFull:
            # 消费者此时不可能阻塞在 get 上；排空后由 recv 的空队列检查抛出终止错误。
            pass
        return True

    def _fail(self, error: TunnelChannelError) -> None:
        if not self._set_terminal(error):
            return
        try:
            self._failure_callback(error)
        except Exception:  # noqa: BLE001
            # A callback must never leak through aiortc's synchronous emitter.
            pass

    def _invoke_lifecycle_callback(self, callback: LifecycleCallback | None) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001
            self._fail(TunnelChannelError("DataChannel lifecycle callback failed"))

    def _terminal_exception(self) -> TunnelChannelError:
        return self._terminal_error or TunnelChannelError("DataChannel closed")

    def _ready_state(self) -> str | None:
        state = getattr(self._raw, "readyState", None)
        return state if isinstance(state, str) else None

    def _buffered_amount(self) -> int:
        value = getattr(self._raw, "bufferedAmount", 0)
        if not isinstance(value, int):
            raise TypeError("DataChannel bufferedAmount must be int")
        return value

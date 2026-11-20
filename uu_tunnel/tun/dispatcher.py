"""一块本机 TUN 与多条隧道之间的 IPv4 分发：按目的地址选隧道，按源地址校验入站。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import Any

from ..webrtc import TunnelChannel
from .config import (
    TUNNEL_MTU,
    TUNNEL_SUPERNET,
    TunnelConfigError,
    TunnelPacketError,
    TunnelStateError,
    validate_packet,
)
from .native import NativeTunDevice


logger = logging.getLogger(__name__)
Report = Callable[[str], None]
DeviceFactory = Callable[..., NativeTunDevice]
_INBOUND_QUEUE_SIZE = 1024


@dataclass
class _Counters:
    rx_packets: int = 0
    rx_bytes: int = 0
    tx_packets: int = 0
    tx_bytes: int = 0
    dropped_packets: int = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "rx_packets": self.rx_packets,
            "rx_bytes": self.rx_bytes,
            "tx_packets": self.tx_packets,
            "tx_bytes": self.tx_bytes,
            "dropped_packets": self.dropped_packets,
        }


class TunRoute:
    """一条隧道在本机 TUN 里的路由：目的 peer_ip 的包发往该隧道，来自该隧道的包只接受 peer_ip → 本机。"""

    __slots__ = ("peer_key", "peer_ip", "channel", "enabled", "counters", "_dispatcher", "_active")

    def __init__(
        self,
        peer_key: str,
        peer_ip: IPv4Address,
        channel: TunnelChannel,
        dispatcher: TunDispatcher,
        *,
        enabled: bool,
    ) -> None:
        self.peer_key = peer_key
        self.peer_ip = peer_ip
        self.channel = channel
        # 对端 TUN 未挂载时不往隧道里发包，直接丢弃计数。
        self.enabled = enabled
        self.counters = _Counters()
        self._dispatcher = dispatcher
        self._active = True

    @property
    def active(self) -> bool:
        return self._active

    async def deliver(self, packet: bytes) -> None:
        """隧道收到的原始包写入本机 TUN；地址不符则计入丢弃。"""

        if not self._active:
            return
        try:
            validate_packet(
                packet,
                mtu=self._dispatcher.mtu,
                expected_source=self.peer_ip,
                expected_destination=self._dispatcher.local_ip,
            )
        except TunnelPacketError:
            self.counters.dropped_packets += 1
            return
        await self._dispatcher._enqueue(self, packet)


class TunDispatcher:
    """持有唯一的本机 TUN 设备与全部路由；不感知隧道生命周期。"""

    def __init__(
        self,
        local_ip: IPv4Address,
        *,
        mtu: int = TUNNEL_MTU,
        report: Report = print,
        device_factory: DeviceFactory | None = None,
    ) -> None:
        if not isinstance(local_ip, IPv4Address) or local_ip not in TUNNEL_SUPERNET:
            raise TunnelConfigError("tunnel local address is invalid")
        if type(mtu) is not int or not 576 <= mtu <= TUNNEL_MTU:
            raise TunnelConfigError("dispatcher MTU must be 576..1280")
        self.local_ip = local_ip
        self.mtu = mtu
        self._report = report
        self._device_factory = device_factory or NativeTunDevice
        self._device: NativeTunDevice | None = None
        self._outbound_task: asyncio.Task[None] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._inbound_queue: asyncio.Queue[tuple[TunRoute, bytes]] = asyncio.Queue(
            maxsize=_INBOUND_QUEUE_SIZE
        )
        self._routes_by_ip: dict[IPv4Address, TunRoute] = {}
        self._routes_by_peer: dict[str, TunRoute] = {}
        self._counters = _Counters()
        self._lifecycle_lock = asyncio.Lock()
        self._stop_task: asyncio.Task[None] | None = None
        self._fatal: Exception | None = None
        self._fatal_event = asyncio.Event()

    @property
    def device(self) -> NativeTunDevice | None:
        return self._device

    @property
    def fatal(self) -> Exception | None:
        """返回让本机 TUN 泵终止的异常。"""

        return self._fatal

    @property
    def is_running(self) -> bool:
        return self._device is not None and self._fatal is None

    @property
    def routes(self) -> tuple[TunRoute, ...]:
        return tuple(self._routes_by_peer.values())

    async def wait_fatal(self) -> Exception:
        await self._fatal_event.wait()
        fatal = self._fatal
        if fatal is None:
            raise AssertionError("dispatcher fatal event has no exception")
        return fatal

    async def start(self) -> None:
        """打开本机 TUN 并启动出站/写入泵。"""

        async with self._lifecycle_lock:
            if self._device is not None:
                raise TunnelStateError("TUN is already mounted")
            self._stop_task = None
            self._fatal = None
            self._fatal_event.clear()
            device = self._device_factory(self.local_ip, self.mtu)
            try:
                await device.start()
            except BaseException:
                try:
                    await device.close()
                except Exception:
                    logger.warning("failed to close TUN after start failure", exc_info=True)
                raise
            self._device = device
            self._outbound_task = asyncio.create_task(self._pump_outbound(), name="uu-tunnel-tun-outbound")
            self._writer_task = asyncio.create_task(self._pump_writer(), name="uu-tunnel-tun-writer")

    async def stop(self) -> None:
        """停止泵、清空路由并关闭本机 TUN；幂等。"""

        task = self._stop_task
        if task is None:
            task = asyncio.create_task(self._stop_once(), name="uu-tunnel-tun-stop")
            self._stop_task = task
        await _await_cleanup(task)

    def add_route(
        self,
        peer_key: str,
        peer_ip: IPv4Address,
        channel: TunnelChannel,
        *,
        enabled: bool = True,
    ) -> TunRoute:
        """登记一条隧道路由；地址与本机相同或已被其他隧道占用时拒绝。"""

        key = _validate_peer_key(peer_key)
        if not self.is_running:
            raise TunnelStateError("TUN is not mounted")
        if not isinstance(peer_ip, IPv4Address) or peer_ip not in TUNNEL_SUPERNET:
            raise TunnelConfigError("peer tunnel address is invalid")
        if peer_ip == self.local_ip:
            raise TunnelConfigError("对端隧道地址与本机相同")
        if key in self._routes_by_peer:
            raise TunnelConfigError("tunnel peer already has a route")
        if peer_ip in self._routes_by_ip:
            raise TunnelConfigError(f"对端地址 {peer_ip} 已被其他隧道占用")
        route = TunRoute(key, peer_ip, channel, self, enabled=enabled)
        self._routes_by_ip[peer_ip] = route
        self._routes_by_peer[key] = route
        return route

    def remove_route(self, peer_key: str) -> None:
        """移除一条路由；不存在时无副作用。"""

        route = self._routes_by_peer.pop(peer_key, None)
        if route is None:
            return
        route._active = False
        if self._routes_by_ip.get(route.peer_ip) is route:
            self._routes_by_ip.pop(route.peer_ip, None)

    def route(self, peer_key: str) -> TunRoute | None:
        return self._routes_by_peer.get(peer_key)

    def stats(self) -> dict[str, Any]:
        return {
            "global": self._counters.snapshot(),
            "routes": {
                route.peer_key: {"peer_ip": str(route.peer_ip), **route.counters.snapshot()}
                for route in self._routes_by_peer.values()
            },
        }

    async def _enqueue(self, route: TunRoute, packet: bytes) -> None:
        if self._device is None or self._fatal is not None:
            route.counters.dropped_packets += 1
            return
        await self._inbound_queue.put((route, packet))

    async def _pump_outbound(self) -> None:
        device = self._require_device()
        try:
            while True:
                packet = await device.read_packet()
                try:
                    validate_packet(packet, mtu=self.mtu, expected_source=self.local_ip)
                except TunnelPacketError:
                    self._counters.dropped_packets += 1
                    continue
                route = self._routes_by_ip.get(IPv4Address(packet[16:20]))
                if route is None or not route.enabled:
                    self._counters.dropped_packets += 1
                    if route is not None:
                        route.counters.dropped_packets += 1
                    continue
                try:
                    await route.channel.send_packet(packet)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # 隧道通道已失效：隧道自身随会话结束，这里只丢包。
                    route.counters.dropped_packets += 1
                    continue
                self._counters.tx_packets += 1
                self._counters.tx_bytes += len(packet)
                route.counters.tx_packets += 1
                route.counters.tx_bytes += len(packet)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._mark_fatal(exc)

    async def _pump_writer(self) -> None:
        device = self._require_device()
        try:
            while True:
                route, packet = await self._inbound_queue.get()
                try:
                    if not route.active:
                        continue
                    await device.write_packet(packet)
                    self._counters.rx_packets += 1
                    self._counters.rx_bytes += len(packet)
                    route.counters.rx_packets += 1
                    route.counters.rx_bytes += len(packet)
                finally:
                    self._inbound_queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._discard_inbound_queue()
            await self._mark_fatal(exc)

    async def _mark_fatal(self, failure: Exception) -> None:
        if self._fatal is not None:
            return
        self._fatal = failure
        self._fatal_event.set()
        self._safe_report(f"TUN pump stopped: {failure}")

    async def _stop_once(self) -> None:
        async with self._lifecycle_lock:
            outbound = self._outbound_task
            writer = self._writer_task
            device = self._device
            self._outbound_task = None
            self._writer_task = None
            self._device = None
            for key in tuple(self._routes_by_peer):
                self.remove_route(key)
            await _cancel_task(outbound)
            self._discard_inbound_queue()
            await _cancel_task(writer)
            if device is not None:
                await device.close()

    def _discard_inbound_queue(self) -> None:
        while True:
            try:
                self._inbound_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._inbound_queue.task_done()

    def _require_device(self) -> NativeTunDevice:
        device = self._device
        if device is None:
            raise TunnelStateError("TUN is not mounted")
        return device

    def _safe_report(self, message: str) -> None:
        try:
            self._report(message)
        except Exception:
            logger.warning("dispatcher report callback failed", exc_info=True)


def _validate_peer_key(peer_key: str) -> str:
    if not isinstance(peer_key, str) or not peer_key or peer_key != peer_key.strip():
        raise TunnelConfigError("tunnel peer key is invalid")
    return peer_key


async def _cancel_task(task: asyncio.Task[None] | None) -> None:
    if task is None or task is asyncio.current_task():
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    except Exception:
        logger.warning("dispatcher pump cleanup failed", exc_info=True)


async def _await_cleanup(task: asyncio.Task[None]) -> None:
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.done():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


__all__ = ["TunDispatcher", "TunRoute"]

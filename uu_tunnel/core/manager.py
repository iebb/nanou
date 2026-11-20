"""进程内统一的隧道编排入口：隧道、被控服务、本机 TUN 与端口映射。

隧道 = 一条 WebRTC 会话：数据通道传原始 IPv4 包，控制通道传 JSON（hello / tun 状态 / 迁移）。
TUN 是本机独立资源：挂载时为每条已知对端地址的隧道装路由，卸载时全部拆掉，隧道本身不受影响；
对端 TUN 未挂载时本机往该隧道的包直接丢弃。被控端每接入一个控制端就把隧道交给独立任务并立即
重建房间，因此可同时被多条隧道连接。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import socket
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from ipaddress import IPv4Address
from typing import Any

from .. import control_proto
from .. import workflows as workflow
from ..access import (
    CONNECT_TYPE_BY_CONFIRMATION,
    CONNECT_TYPE_BY_PASSWORD,
    CONNECT_TYPE_PASSWORD_CONFIRMATION,
)
from ..api import (
    RoomConfig,
    UuApiClient,
    UuApiCommittedResponseError,
    UuApiError,
    UuApiTransientError,
    UuApiUnknownOutcomeError,
    UuAuthExpiredError,
)
from ..profile import stable_machine_identity
from ..tun import TUNNEL_MTU, TUNNEL_SUPERNET, NativeTunDevice, TunDispatcher, TunRoute, local_tunnel_address
from ..tun.config import TunnelConfigError, TunnelStateError, parse_tunnel_address
from ..webrtc import (
    TunnelChannel,
    TunnelRestartRequired,
    TunnelSession,
    TunnelSessionError,
)
from .events import (
    GUEST_MODES,
    GUEST_MODE_CODE,
    EventBus,
    HandoffState,
    RoomPublished,
    TunFatal,
    TunnelClosed,
    TunnelEstablished,
    TunnelFailed,
    TunnelInfo,
    TunnelUpdated,
)
from .forward import ForwardAcceptor, ForwardListener, ForwardSpec


logger = logging.getLogger(__name__)
Report = Callable[[str], None]
SessionFactory = Callable[..., TunnelSession]
JoinFactory = Callable[[str, bool], Awaitable[RoomConfig | dict[str, Any] | Any]]
GuestClientFactory = Callable[[], UuApiClient]
Preflight = Callable[[], None]
ServiceKey = tuple[str, str]
_CONTROL_MODE_TASK_LIMIT = 8
# 单房间最多记住 256 个 control_id；足以覆盖正常重试，同时约束恶意 push 内存。
_CONTROL_MODE_ID_BUDGET = 256
CONTROL_CHANNEL_TIMEOUT_S = 10.0
HELLO_TIMEOUT_S = 15.0
HANDOFF_ROOM_TIMEOUT_S = 30.0
HANDOFF_JOIN_TIMEOUT_S = 60.0
HANDOFF_COMMIT_TIMEOUT_S = 15.0
HANDOFF_TOTAL_TIMEOUT_S = 120.0
HANDOFF_RETIRE_DELAY_S = 10.0
# 确认式协助最多等对方确认这么久；否则 ("peer", connect_id) 会被无限期占位，用户无法重试也无法断开。
ASSIST_CONFIRMATION_TIMEOUT_S = 120.0


class TunnelProtocolMismatch(TunnelSessionError):
    """对端没有按控制协议交换 hello。"""


class _ControlModeBudgetExceeded(TunnelSessionError):
    """游客房间收到超出预算的 control-mode ID。"""


class _TunnelState(str, Enum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    CLOSING = "closing"


@dataclass(eq=False)
class _Tunnel:
    peer_key: str
    role: str
    via: str
    service: str
    session: TunnelSession
    channel: TunnelChannel
    ready: asyncio.Future[TunnelInfo]
    ctl: TunnelChannel | None = None
    peer_ip: IPv4Address | None = None
    peer_name: str = ""
    tun_peer: bool = False
    route: TunRoute | None = None
    tun_error: str = ""
    state: _TunnelState = _TunnelState.CONNECTING
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    reader: asyncio.Task[None] | None = None
    control: asyncio.Task[None] | None = None
    # 迁移会替换 session/channel/ctl；等待旧会话关闭的循环据此判断是替换还是结束。
    generation: int = 0
    # 迁移进行中时清空：旧传输此时失效不算隧道结束，读泵与结束判定都等迁移收尾后再看代际。
    handoff_idle: asyncio.Event = field(default_factory=lambda: _set_event())
    # 控制泵把发给 handoff() 的响应放到这里，避免两个读者争抢同一控制通道。
    inbox: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)


@dataclass(slots=True)
class _ServiceOwner:
    stop_event: asyncio.Event
    task: asyncio.Task[None]


@dataclass(slots=True)
class _HandoffGuestRoom:
    connect_id: str
    access_code: str
    session: TunnelSession
    client: UuApiClient
    join_task: asyncio.Task[tuple[TunnelSession, TunnelChannel]]
    auxiliary_tasks: set[asyncio.Task[Any]]
    _joined: bool = False

    async def join(self) -> tuple[TunnelSession, TunnelChannel]:
        if self._joined:
            raise TunnelSessionError("一次性迁移房已被使用")
        self._joined = True
        return await self.join_task

    async def close(self, *, keep_session: bool = False) -> None:
        if not self.join_task.done():
            self.join_task.cancel()
        for task in tuple(self.auxiliary_tasks):
            if not task.done():
                task.cancel()
        await asyncio.gather(self.join_task, *tuple(self.auxiliary_tasks), return_exceptions=True)
        if not keep_session:
            await workflow._stop_session_owner_shielded(self.session)
        await self.client.close()


class _ControlModeResponder:
    """常规 guest 与 handoff guest 共用的 control_id 有界准入。"""

    def __init__(
        self,
        client: UuApiClient,
        control_mode: str,
        stop_event: asyncio.Event,
        report: Report,
        track: Callable[[asyncio.Task[Any]], None],
    ) -> None:
        self._client = client
        self._control_mode = control_mode
        self._stop_event = stop_event
        self._report = report
        self._track = track
        self.ids: set[str] = set()
        self.tasks: set[asyncio.Task[Any]] = set()
        self.overloaded = asyncio.Event()

    def handle(self, control_id: str) -> None:
        if not control_id or control_id in self.ids or self.overloaded.is_set():
            return
        if len(self.ids) >= _CONTROL_MODE_ID_BUDGET:
            self.overloaded.set()
            return
        self.ids.add(control_id)
        if len(self.tasks) >= _CONTROL_MODE_TASK_LIMIT:
            return
        task = asyncio.create_task(
            workflow._answer_guest_control_mode(
                self._client, control_id, self._control_mode, self._stop_event, self._report
            ),
            name=f"uu-tunnel-control-mode-{control_id}",
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        self._track(task)


class TunnelManager:
    """统一管理控制端连接、被控房间、本机 TUN 与端口映射。"""

    def __init__(
        self,
        client: UuApiClient | None = None,
        *,
        events: EventBus | None = None,
        session_factory: SessionFactory = TunnelSession,
        join_factory: JoinFactory | None = None,
        guest_client_factory: GuestClientFactory = UuApiClient,
        preflight: Preflight = NativeTunDevice.preflight,
        report: Report = print,
        local_ip: IPv4Address | None = None,
        device_factory: Callable[..., NativeTunDevice] | None = None,
        forward_acceptor: ForwardAcceptor | None = None,
        handoff_join_factory: Callable[
            [str, str, asyncio.Event], Awaitable[tuple[TunnelSession, TunnelChannel]]
        ]
        | None = None,
    ) -> None:
        self.client = client
        self.events = events if events is not None else EventBus()
        self.local_ip = local_ip if local_ip is not None else local_tunnel_address(stable_machine_identity())
        self._session_factory = session_factory
        self._join_factory = join_factory
        self._guest_client_factory = guest_client_factory
        self._preflight = preflight
        self._report = report
        self._handoff_join_factory = handoff_join_factory
        self._tunnels: dict[str, _Tunnel] = {}
        self._services: dict[ServiceKey, _ServiceOwner] = {}
        self._lock = asyncio.Lock()
        self._tun = TunDispatcher(self.local_ip, report=report, device_factory=device_factory)
        self._tun_lock = asyncio.Lock()
        self._tun_watch: asyncio.Task[None] | None = None
        self._forward_lock = asyncio.Lock()
        self._forwards: dict[str, ForwardListener] = {}
        self._forward_acceptor = forward_acceptor if forward_acceptor is not None else ForwardAcceptor()
        self._inbound_seq = 0
        self._background: set[asyncio.Task[None]] = set()
        self._accepting = True
        self._stop_task: asyncio.Task[None] | None = None

    # ---- 控制端 ----

    async def connect_device(
        self, device_id: str, *, force: bool = False, force_relay: bool = False
    ) -> TunnelInfo:
        """连接一个账号设备。"""

        peer_key = workflow._required_id(device_id, "device_id")

        async def prepare(stop_event: asyncio.Event) -> tuple[TunnelSession, TunnelChannel]:
            return await self._prepare_device(peer_key, force=force, force_relay=force_relay, stop_event=stop_event)

        return await self._connect(peer_key, prepare, via="account")

    async def connect_devices(
        self, device_ids: Iterable[str], *, force: bool = False, force_relay: bool = False
    ) -> list[TunnelInfo]:
        """并行连接多个账号设备，返回与输入顺序一致的结果。"""

        return list(
            await asyncio.gather(
                *(
                    self.connect_device(device_id, force=force, force_relay=force_relay)
                    for device_id in list(device_ids)
                )
            )
        )

    async def assist_by_code(self, connect_id: str, code: str, *, force_relay: bool = False) -> TunnelInfo:
        """通过访问码加入协助房间。"""

        peer_key = workflow._required_id(connect_id, "connect_id")
        access_code = workflow._validated_access_code(code)

        async def prepare(stop_event: asyncio.Event) -> tuple[TunnelSession, TunnelChannel]:
            return await self._prepare_assist_by_code(
                peer_key, access_code, force_relay=force_relay, stop_event=stop_event
            )

        return await self._connect(peer_key, prepare, via="guest")

    async def assist_by_confirmation(self, connect_id: str, *, force_relay: bool = False) -> TunnelInfo:
        """通过确认轮询加入协助房间。"""

        peer_key = workflow._required_id(connect_id, "connect_id")

        async def prepare(stop_event: asyncio.Event) -> tuple[TunnelSession, TunnelChannel]:
            return await self._prepare_assist_by_confirmation(peer_key, force_relay=force_relay, stop_event=stop_event)

        return await self._connect(peer_key, prepare, via="guest")

    async def _connect(
        self,
        peer_key: str,
        prepare: Callable[[asyncio.Event], Awaitable[tuple[TunnelSession, TunnelChannel]]],
        *,
        via: str,
    ) -> TunnelInfo:
        async with self._lock:
            if not self._accepting:
                raise TunnelSessionError("tunnel manager is stopping")
            existing = self._tunnels.get(peer_key)
            if existing is not None:
                if existing.state is _TunnelState.CONNECTING:
                    raise TunnelSessionError(f"{peer_key} 已在连接")
                if existing.state is _TunnelState.CLOSING:
                    raise TunnelSessionError(f"{peer_key} 正在关闭，请稍后重试")
                raise TunnelSessionError(f"{peer_key} 已连接")
        stop_event = asyncio.Event()
        prepare_task = asyncio.create_task(prepare(stop_event), name=f"uu-tunnel-prepare-{peer_key}")
        try:
            session, channel = await asyncio.shield(prepare_task)
        except asyncio.CancelledError:
            stop_event.set()
            prepare_task.cancel()
            await _drain_task(prepare_task)
            raise
        return await self._start_tunnel(peer_key, session, channel, role="controller", via=via)

    async def disconnect(self, peer_key: str) -> bool:
        """停止一条隧道并等待清理完成；返回是否确有隧道被停止。"""

        key = workflow._required_id(peer_key, "peer_key")
        async with self._lock:
            tunnel = self._tunnels.get(key)
            if tunnel is None:
                return False
            tunnel.stop_event.set()
            task = tunnel.task
        if task is not None:
            await _drain_task(task)
        return True

    async def disconnect_all(self) -> None:
        async with self._lock:
            keys = tuple(self._tunnels)
        if keys:
            await asyncio.gather(*(self.disconnect(key) for key in keys))

    def tunnels(self) -> list[TunnelInfo]:
        """返回全部隧道的当前快照（含正在连接与关闭中的）。"""

        return [self._info(tunnel) for tunnel in self._tunnels.values()]

    async def stop(self) -> None:
        """禁止新连接并幂等停止全部编排资源。"""

        task = self._stop_task
        if task is None:
            task = asyncio.create_task(self._stop_once(), name="uu-tunnel-manager-stop")
            self._stop_task = task
        await _await_cleanup(task)

    # ---- TUN ----

    async def mount_tun(self) -> None:
        """挂载本机 TUN 并为全部已连接隧道装路由；无管理员权限时由预检抛错。"""

        async with self._tun_lock:
            if self._tun.is_running:
                return
            if self._tun_watch is not None:
                await _cancel_task(self._tun_watch)
                self._tun_watch = None
            await self._tun.stop()
            self._preflight()
            await self._tun.start()
            self._tun_watch = asyncio.create_task(self._watch_tun_fatal(), name="uu-tunnel-tun-watch")
            for tunnel in self._tunnels.values():
                if tunnel.state is _TunnelState.CONNECTED:
                    self._attach_route(tunnel)
        await self._announce_tun_state(True)

    async def unmount_tun(self) -> None:
        """卸载本机 TUN；隧道保持连接，只是不再收发三层流量。"""

        async with self._tun_lock:
            if self._tun_watch is not None:
                await _cancel_task(self._tun_watch)
                self._tun_watch = None
            await self._tun.stop()
            for tunnel in self._tunnels.values():
                tunnel.route = None
                tunnel.tun_error = ""
        await self._announce_tun_state(False)

    def tun_status(self) -> dict[str, Any]:
        fatal = self._tun.fatal
        return {
            "mounted": self._tun.is_running,
            "address": f"{self.local_ip}/{TUNNEL_SUPERNET.prefixlen}",
            "mtu": TUNNEL_MTU,
            "fatal": _exception_text(fatal) if fatal is not None else "",
            "routes": len(self._tun.routes),
        }

    async def _watch_tun_fatal(self) -> None:
        try:
            failure = await self._tun.wait_fatal()
        except asyncio.CancelledError:
            raise
        async with self._tun_lock:
            self._tun_watch = None
            await self._tun.stop()
            for tunnel in self._tunnels.values():
                tunnel.route = None
        await self.events.emit(TunFatal(_exception_text(failure)))
        await self._announce_tun_state(False)

    def _attach_route(self, tunnel: _Tunnel) -> None:
        tunnel.route = None
        tunnel.tun_error = ""
        if not self._tun.is_running or tunnel.peer_ip is None:
            return
        try:
            tunnel.route = self._tun.add_route(
                tunnel.peer_key, tunnel.peer_ip, tunnel.channel, enabled=tunnel.tun_peer
            )
        except (TunnelConfigError, TunnelStateError) as exc:
            tunnel.tun_error = _exception_text(exc)

    def _detach_route(self, tunnel: _Tunnel) -> None:
        self._tun.remove_route(tunnel.peer_key)
        tunnel.route = None

    async def _announce_tun_state(self, mounted: bool) -> None:
        """把本机 TUN 状态告知全部对端，并发布每条隧道的更新快照。"""

        text = control_proto.encode(control_proto.tun_state(mounted))
        for tunnel in tuple(self._tunnels.values()):
            if tunnel.state is not _TunnelState.CONNECTED or tunnel.ctl is None:
                continue
            try:
                tunnel.ctl.send_text(text)
            except Exception:
                logger.debug("tun state announce failed for %s", tunnel.peer_key)
            await self.events.emit(TunnelUpdated(self._info(tunnel)))

    # ---- 端口映射（两端都可发起）----

    async def add_forward(
        self,
        peer_key: str,
        *,
        listen_port: int,
        target_host: str,
        target_port: int,
        listen_host: str = "127.0.0.1",
    ) -> ForwardSpec:
        """在一条已连接隧道上挂一个本机 TCP listener，连接经隧道由对端转到目标。"""

        async with self._lock:
            tunnel = self._tunnels.get(peer_key)
            if tunnel is None or tunnel.state is not _TunnelState.CONNECTED:
                raise TunnelSessionError(f"隧道 {peer_key} 不存在或未连接")
            session = tunnel.session
        async with self._forward_lock:
            if listen_port != 0 and any(
                existing.spec.listen_host == listen_host and existing.spec.listen_port == listen_port
                for existing in self._forwards.values()
            ):
                raise TunnelSessionError(f"本地端口 {listen_port} 已在转发")
            while True:
                forward_id = secrets.token_hex(4)
                if forward_id not in self._forwards:
                    break
            spec = ForwardSpec(
                forward_id=forward_id,
                peer_key=peer_key,
                listen_host=listen_host,
                listen_port=listen_port,
                target_host=target_host,
                target_port=target_port,
            )
            listener = ForwardListener(spec, session)
            self._forwards[forward_id] = listener
        try:
            await listener.start()
        except OSError as exc:
            await self._drop_forward(forward_id, listener)
            raise TunnelSessionError(f"本地端口 {listen_port} 监听失败") from exc
        except BaseException:
            await self._drop_forward(forward_id, listener)
            raise
        async with self._lock:
            current = self._tunnels.get(peer_key)
            if current is not tunnel or tunnel.state is not _TunnelState.CONNECTED:
                failure = True
            else:
                failure = False
                if listener.session is not tunnel.session:
                    listener.rebind_session(tunnel.session)
        if failure:
            await self._drop_forward(forward_id, listener)
            await listener.close()
            raise TunnelSessionError(f"隧道 {peer_key} 正在关闭")
        return listener.spec

    async def _drop_forward(self, forward_id: str, listener: ForwardListener) -> None:
        async with self._forward_lock:
            if self._forwards.get(forward_id) is listener:
                self._forwards.pop(forward_id)

    async def remove_forward(self, peer_key: str, identifier: str | int) -> ForwardSpec:
        """按 forward_id 或本地端口移除一个映射。"""

        async with self._forward_lock:
            listener = next(
                (
                    item
                    for item in self._forwards.values()
                    if item.spec.peer_key == peer_key
                    and (item.spec.forward_id == identifier or item.spec.listen_port == identifier)
                ),
                None,
            )
            if listener is None:
                raise TunnelSessionError("未找到端口映射")
            self._forwards.pop(listener.spec.forward_id, None)
        await listener.close()
        return listener.spec

    async def list_forwards(self) -> list[ForwardSpec]:
        async with self._forward_lock:
            return [listener.spec for listener in self._forwards.values()]

    # ---- 迁移：把账号直连隧道的传输换到对端开出的一次性协助会话上 ----

    async def handoff(self, peer_key: str) -> TunnelInfo:
        key = workflow._required_id(peer_key, "peer_key")
        task = asyncio.create_task(self._handoff_once(key), name=f"uu-tunnel-handoff-{key}")
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=HANDOFF_TOTAL_TIMEOUT_S)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            task.cancel()
            await _drain_task(task)
            raise

    async def _handoff_once(self, key: str) -> TunnelInfo:
        async with self._lock:
            tunnel = self._tunnels.get(key)
            if (
                tunnel is None
                or tunnel.state is not _TunnelState.CONNECTED
                or tunnel.role != "controller"
                or tunnel.via != "account"
                or tunnel.ctl is None
            ):
                raise TunnelSessionError("仅本机为主控的账号直连隧道支持迁移")
            if not tunnel.handoff_idle.is_set():
                raise TunnelSessionError(f"{key} 已在迁移")
            tunnel.handoff_idle.clear()
        request_id = secrets.token_hex(12)
        new_session: TunnelSession | None = None
        try:
            await self.events.emit(HandoffState(key, "requested"))
            while not tunnel.inbox.empty():
                tunnel.inbox.get_nowait()
            tunnel.ctl.send_text(control_proto.encode(control_proto.handoff_request(request_id)))
            response = await asyncio.wait_for(
                _inbox_message(tunnel, request_id, {control_proto.HANDOFF_ROOM, control_proto.HANDOFF_ERROR}),
                timeout=HANDOFF_ROOM_TIMEOUT_S,
            )
            if response["type"] == control_proto.HANDOFF_ERROR:
                raise TunnelSessionError(str(response["reason"]))
            await self.events.emit(HandoffState(key, "room_ready"))
            stop_event = asyncio.Event()
            if self._handoff_join_factory is not None:
                new_session, new_channel = await self._handoff_join_factory(
                    str(response["connect_id"]), str(response["access_code"]), stop_event
                )
            else:
                new_session, new_channel = await self._prepare_assist_by_code(
                    str(response["connect_id"]),
                    str(response["access_code"]),
                    force_relay=False,
                    stop_event=stop_event,
                )
            new_ctl = await new_session.wait_control_channel(timeout=CONTROL_CHANNEL_TIMEOUT_S)
            peer_ip, _name, tun_peer = await self._exchange_hello(new_ctl)
            if peer_ip != tunnel.peer_ip:
                raise TunnelSessionError("迁移会话的对端地址与原隧道不一致")
            await self.events.emit(HandoffState(key, "prepared"))
            # 对端提交后立刻改走新通道发包；切换前先替新通道收包，免得它入队溢出把新会话终止。
            drain = asyncio.create_task(self._drain_channel(tunnel, new_channel), name=f"uu-tunnel-handoff-drain-{key}")
            try:
                new_ctl.send_text(control_proto.encode(control_proto.handoff_commit(request_id)))
                reply = await asyncio.wait_for(
                    _recv_handoff(
                        new_ctl,
                        request_id,
                        {control_proto.HANDOFF_COMMITTED, control_proto.HANDOFF_ERROR},
                        on_other=lambda message: self._apply_control(tunnel, message),
                    ),
                    timeout=HANDOFF_COMMIT_TIMEOUT_S,
                )
            finally:
                await _cancel_task(drain)
            if reply["type"] == control_proto.HANDOFF_ERROR:
                raise TunnelSessionError(str(reply["reason"]))
            await self.events.emit(HandoffState(key, "committed"))
            async with self._lock:
                if self._tunnels.get(key) is not tunnel or tunnel.state is not _TunnelState.CONNECTED:
                    raise TunnelSessionError("迁移期间原隧道已关闭")
                old = self._swap_transport(tunnel, new_session, new_channel, new_ctl, tun_peer=tun_peer)
            new_session = None
            # 控制端是唯一主动关闭旧会话的一方：对端提交后只 detach，等这里切换完再关。
            await workflow._stop_session_owner_shielded(old[0])
            await self.events.emit(TunnelUpdated(self._info(tunnel)))
            await self.events.emit(HandoffState(key, "completed"))
            return self._info(tunnel)
        except BaseException as exc:
            if new_session is not None:
                await workflow._stop_session_owner_shielded(new_session)
            await self.events.emit(HandoffState(key, "failed", _exception_text(exc)))
            raise
        finally:
            tunnel.handoff_idle.set()

    async def _serve_handoff(self, tunnel: _Tunnel, request_id: str) -> None:
        """被控端处理迁移请求：开一次性协助房，等控制端加入并提交，然后切换传输。"""

        assert tunnel.ctl is not None
        if not tunnel.handoff_idle.is_set():
            return
        tunnel.handoff_idle.clear()
        room: _HandoffGuestRoom | None = None
        new_session: TunnelSession | None = None
        new_ctl: TunnelChannel | None = None
        swapped = False
        try:
            room = await self._create_handoff_guest_room()
            tunnel.ctl.send_text(
                control_proto.encode(control_proto.handoff_room(request_id, room.connect_id, room.access_code))
            )
            new_session, new_channel = await asyncio.wait_for(room.join(), timeout=HANDOFF_JOIN_TIMEOUT_S)
            new_ctl = await new_session.wait_control_channel(timeout=CONTROL_CHANNEL_TIMEOUT_S)
            peer_ip, _name, tun_peer = await self._exchange_hello(new_ctl)
            if peer_ip != tunnel.peer_ip:
                raise TunnelSessionError("迁移会话的对端地址与原隧道不一致")
            await asyncio.wait_for(
                _recv_handoff(
                    new_ctl,
                    request_id,
                    {control_proto.HANDOFF_COMMIT},
                    on_other=lambda message: self._apply_control(tunnel, message),
                ),
                timeout=HANDOFF_ROOM_TIMEOUT_S,
            )
            old = self._swap_transport(tunnel, new_session, new_channel, new_ctl, tun_peer=tun_peer)
            swapped = True
            new_ctl.send_text(control_proto.encode(control_proto.handoff_committed(request_id)))
            # 旧会话交给控制端在其切换后关闭，避免它先看到旧传输断开而把隧道当成"对端已断开"；
            # 控制端迟迟不关时这里兜底延迟关闭。
            self._retire_later(old[0])
            await self.events.emit(TunnelUpdated(self._info(tunnel)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._safe_report(f"handoff failed: {_exception_text(exc)}")
            try:
                (new_ctl if new_ctl is not None and not swapped else tunnel.ctl).send_text(
                    control_proto.encode(control_proto.handoff_error(request_id, _exception_text(exc)))
                )
            except Exception:
                pass
        finally:
            tunnel.handoff_idle.set()
            if room is not None:
                await room.close(keep_session=swapped)

    def _retire_later(self, session: TunnelSession) -> None:
        async def retire() -> None:
            await asyncio.sleep(HANDOFF_RETIRE_DELAY_S)
            await workflow._stop_session_owner_shielded(session)

        task = asyncio.create_task(retire(), name="uu-tunnel-handoff-retire")
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(_consume_task_result)

    def _swap_transport(
        self,
        tunnel: _Tunnel,
        session: TunnelSession,
        channel: TunnelChannel,
        ctl: TunnelChannel,
        *,
        tun_peer: bool,
    ) -> tuple[TunnelSession, TunnelChannel, TunnelChannel | None]:
        """同步切换隧道的传输；旧通道立即 detach 让读泵转到新通道。"""

        old = (tunnel.session, tunnel.channel, tunnel.ctl)
        tunnel.generation += 1
        tunnel.session = session
        tunnel.channel = channel
        tunnel.ctl = ctl
        tunnel.via = "guest"
        tunnel.tun_peer = tun_peer
        if tunnel.route is not None:
            tunnel.route.channel = channel
            tunnel.route.enabled = tun_peer
        for listener in self._forwards.values():
            if listener.spec.peer_key == tunnel.peer_key:
                listener.rebind_session(session)
        old[1].detach()
        if old[2] is not None:
            old[2].detach()
        self._register_forward_handler(session)
        return old

    # ---- 被控服务 ----

    async def serve_account(self, *, stop_event: asyncio.Event | None = None, peer_key: str = "serve") -> None:
        """持续托管账号设备房间：每接入一个控制端就重建房间。"""

        client = self._require_client()
        event = stop_event if stop_event is not None else asyncio.Event()
        key = workflow._required_id(peer_key, "peer_key")
        await self._run_service(
            event, self._serve_account_loop(client, event, key), service_key=("account", key), name="uu-tunnel-serve-account"
        )

    async def serve_guest(
        self,
        access_code: str | None = None,
        *,
        mode: str = GUEST_MODE_CODE,
        approve_request: workflow.ApprovalRequest | None = None,
        auto_approve: bool = False,
        stop_event: asyncio.Event | None = None,
        peer_key: str = "guest",
    ) -> None:
        """持续托管协助房间。

        mode=code：房间只接受凭访问码加入（access_code 为空则每次生成）；
        mode=confirm：房间只接受确认式加入，由 approve_request 决定，auto_approve 时自动允许。
        """

        event = stop_event if stop_event is not None else asyncio.Event()
        key = workflow._required_id(peer_key, "peer_key")
        await self._run_service(
            event,
            self._serve_guest_loop(
                access_code, mode=mode, approve_request=approve_request, auto_approve=auto_approve, stop_event=event, peer_key=key
            ),
            service_key=("guest", key),
            name="uu-tunnel-serve-guest",
        )

    async def _run_service(
        self, stop_event: asyncio.Event, awaitable: Awaitable[None], *, service_key: ServiceKey, name: str
    ) -> None:
        async with self._lock:
            if not self._accepting or service_key in self._services:
                if hasattr(awaitable, "close"):
                    awaitable.close()  # type: ignore[attr-defined]
                if not self._accepting:
                    raise TunnelSessionError("tunnel manager is stopping")
                kind, peer_key = service_key
                raise TunnelSessionError(f"{kind} service {peer_key} is already running")
            task = asyncio.create_task(awaitable, name=name)
            owner = _ServiceOwner(stop_event=stop_event, task=task)
            self._services[service_key] = owner
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            stop_event.set()
            if not task.done():
                task.cancel()
            await _drain_task(task)
            raise
        finally:
            async with self._lock:
                if self._services.get(service_key) is owner:
                    self._services.pop(service_key, None)
                keys = (
                    tuple(key for key, tunnel in self._tunnels.items() if tunnel.service == service_key[1])
                    if stop_event.is_set()
                    else ()
                )
            # 主动停服时其接入的全部隧道一并结束；服务自身崩溃重试不影响已建隧道。
            if keys:
                await workflow._await_owner_shielded(asyncio.gather(*(self.disconnect(key) for key in keys)))

    async def _serve_account_loop(self, client: UuApiClient, stop_event: asyncio.Event, peer_key: str) -> None:
        client.require_account_identity()
        if stop_event.is_set():
            return
        own_device_id = await workflow._await_api_task(asyncio.create_task(client.ensure_device()), stop_event)
        if own_device_id is None:
            return
        self._safe_report(f"account device ready: {own_device_id}")
        failure_count = 0
        while not stop_event.is_set():
            session: TunnelSession | None = None
            room_state = workflow._AccountRoomState.NOT_STARTED
            retry = False
            stopped = False
            accepted = False
            cleanup_ok = True
            try:
                room_state = workflow._AccountRoomState.IN_FLIGHT
                upstream = await workflow._await_api_task(
                    asyncio.create_task(client.create_room()), stop_event, commit_sensitive=True
                )
                if upstream is None:
                    room_state = workflow._AccountRoomState.UNKNOWN
                    stopped = True
                else:
                    room_state = workflow._AccountRoomState.COMMITTED
                    room_config = workflow._require_room_config(upstream)
                    if stop_event.is_set():
                        stopped = True
                    else:
                        self._safe_report("room created; connecting signal")
                        session = self._session_factory(room_config, role="controlled")
                        await workflow._await_session_task(
                            session, asyncio.create_task(session.connect_signal()), stop_event
                        )
                        self._safe_report("room ready; waiting for a peer")
                        channel = await workflow._await_session_task(
                            session, asyncio.create_task(session.negotiate()), stop_event
                        )
                        await self._accept_inbound(peer_key, session, channel, via="account")
                        # 隧道已交给独立任务；房间用完即清，下一轮立刻重建以接纳下一个控制端。
                        session = None
                        accepted = True
            except workflow._WorkflowStopped:
                stopped = True
            except UuAuthExpiredError:
                if room_state is workflow._AccountRoomState.IN_FLIGHT:
                    room_state = workflow._AccountRoomState.NOT_STARTED
                raise
            except UuApiCommittedResponseError:
                if room_state is workflow._AccountRoomState.IN_FLIGHT:
                    room_state = workflow._AccountRoomState.UNKNOWN
                retry = True
                self._safe_report("room create committed response unavailable; reconciling")
            except UuApiUnknownOutcomeError:
                if room_state is workflow._AccountRoomState.IN_FLIGHT:
                    room_state = workflow._AccountRoomState.UNKNOWN
                retry = True
                self._safe_report("room create outcome unknown; reconciling")
            except UuApiError:
                if room_state is workflow._AccountRoomState.IN_FLIGHT:
                    room_state = workflow._AccountRoomState.NOT_STARTED
                retry = True
                self._safe_report("room API failure; retrying")
            except TunnelSessionError as exc:
                # 单个对端的协商/握手错误只淘汰这次会话，不能拖垮整个账号服务。
                retry = True
                self._safe_report(f"inbound tunnel failed: {exc}; recreating account room")
                await self.events.emit(TunnelFailed(peer_key, str(exc)))
            except asyncio.CancelledError:
                if room_state is workflow._AccountRoomState.IN_FLIGHT:
                    room_state = workflow._AccountRoomState.UNKNOWN
                stopped = True
                stop_event.set()
            finally:
                try:
                    if session is not None:
                        await workflow._stop_session_owner_shielded(session)
                finally:
                    if room_state in (workflow._AccountRoomState.COMMITTED, workflow._AccountRoomState.UNKNOWN):
                        cleanup_ok = await workflow._await_owner_shielded(
                            workflow._release_account_room(client, stop_event, self._report)
                        )
            if not cleanup_ok:
                reason = "account room cleanup could not be confirmed; service stopped"
                self._safe_report(reason)
                await self.events.emit(TunnelFailed(peer_key, reason))
                if stop_event.is_set():
                    return
                raise TunnelSessionError(reason)
            if stopped or stop_event.is_set():
                return
            if accepted:
                failure_count = 0
            if retry:
                self._safe_report("retrying room after backoff")
                if await workflow._wait_stop(
                    stop_event, workflow._full_jitter_delay(failure_count, cap_s=workflow.ROOM_RETRY_CAP_S)
                ):
                    return
                failure_count += 1

    async def _serve_guest_loop(
        self,
        access_code: str | None,
        *,
        mode: str,
        approve_request: workflow.ApprovalRequest | None,
        auto_approve: bool,
        stop_event: asyncio.Event,
        peer_key: str,
    ) -> None:
        if mode not in GUEST_MODES:
            raise ValueError("guest mode must be code or confirm")
        if type(auto_approve) is not bool:
            raise TypeError("auto_approve must be a bool")
        code_mode = mode == GUEST_MODE_CODE
        if not code_mode and access_code is not None:
            raise ValueError("access_code is only valid in code mode")
        fixed_code = workflow._validated_access_code(access_code) if access_code is not None else None
        # 房间只公告一种加入方式：code 模式拒绝一切确认请求，confirm 模式不公开访问码。
        control_mode = CONNECT_TYPE_BY_PASSWORD if code_mode else CONNECT_TYPE_BY_CONFIRMATION
        approval_callback = approve_request if not code_mode else None
        if code_mode:
            auto_approve = False
        if approval_callback is None:

            async def reject_without_boundary(name: str, control_id: str) -> bool:
                del name, control_id
                self._safe_report("guest confirmation rejected: room does not accept confirmation")
                return False

            approval_callback = reject_without_boundary
        if stop_event.is_set():
            return
        failure_count = 0
        while not stop_event.is_set():
            guest_client = self._guest_client_factory()
            session: TunnelSession | None = None
            retry = False
            stopped = False
            accepted = False
            room_closing = False
            room_tasks: set[asyncio.Task[Any]] = set()
            requests: asyncio.Queue[workflow._GuestAccessRequest] = asyncio.Queue(
                maxsize=workflow._GUEST_REQUEST_QUEUE_SIZE
            )
            pending_password_ids: set[str] = set()
            seen_confirmation_ids: set[str] = set()
            seen_confirmation_order: deque[str] = deque()
            canceled_confirmation_ids: set[str] = set()
            canceled_confirmation_order: deque[str] = deque()
            confirmation_cancel_events: dict[str, asyncio.Event] = {}
            decision_state = workflow._GuestDecisionState()
            try:
                upstream = await workflow._await_api_task(
                    asyncio.create_task(guest_client.create_guest_room()), stop_event, commit_sensitive=True
                )
                if upstream is None:
                    stopped = True
                else:
                    room_config = workflow._require_room_config(upstream)
                    current_code = fixed_code or workflow.generate_access_code()

                    def track_room_task(task: asyncio.Task[Any]) -> None:
                        room_tasks.add(task)

                        def done(completed: asyncio.Task[Any]) -> None:
                            room_tasks.discard(completed)
                            _consume_task_result(completed)

                        task.add_done_callback(done)

                    control_modes = _ControlModeResponder(
                        guest_client, control_mode, stop_event, self._report, track_room_task
                    )

                    def on_control_mode_query(control_id: str) -> None:
                        if not room_closing:
                            control_modes.handle(control_id)

                    def on_password_request(control_id: str, salt: str) -> None:
                        if room_closing or not control_id or not salt or control_id in pending_password_ids:
                            return
                        try:
                            requests.put_nowait(
                                workflow._GuestAccessRequest(workflow._GUEST_REQUEST_PASSWORD, control_id, salt)
                            )
                        except asyncio.QueueFull:
                            return
                        pending_password_ids.add(control_id)

                    def on_assist_request(control_id: str, name: str) -> None:
                        if (
                            room_closing
                            or not control_id
                            or control_id in seen_confirmation_ids
                            or control_id in canceled_confirmation_ids
                        ):
                            return
                        try:
                            requests.put_nowait(
                                workflow._GuestAccessRequest(
                                    workflow._GUEST_REQUEST_CONFIRMATION, control_id, workflow._safe_remote_name(name)
                                )
                            )
                        except asyncio.QueueFull:
                            return
                        workflow._remember_id(seen_confirmation_ids, seen_confirmation_order, control_id)
                        confirmation_cancel_events[control_id] = asyncio.Event()

                    def on_assist_cancel(control_id: str) -> None:
                        if room_closing:
                            return
                        workflow._remember_id(canceled_confirmation_ids, canceled_confirmation_order, control_id)
                        event = confirmation_cancel_events.pop(control_id, None)
                        if event is not None:
                            event.set()
                        if control_id == decision_state.control_id and session is not None:
                            session.cancel_authorization_decision()

                    overload_task = asyncio.create_task(
                        _wait_control_mode_overload(control_modes.overloaded),
                        name=f"uu-tunnel-control-mode-budget-{peer_key}",
                    )
                    track_room_task(overload_task)
                    session = self._session_factory(
                        room_config,
                        role="controlled",
                        assist_password_cb=on_password_request,
                        assist_request_cb=on_assist_request,
                        assist_cancel_cb=on_assist_cancel,
                        assist_control_mode_cb=on_control_mode_query,
                        authorized=False,
                    )
                    await _await_guest_room_phase(
                        workflow._await_session_task(session, asyncio.create_task(session.connect_signal()), stop_event),
                        overload_task,
                    )
                    connect_id = await _await_guest_room_phase(
                        workflow._await_api_task(
                            asyncio.create_task(guest_client.share_guest_room()), stop_event, commit_sensitive=True
                        ),
                        overload_task,
                    )
                    if connect_id is None or stop_event.is_set():
                        stopped = True
                    else:
                        if code_mode:
                            start_line = f"remote assistance ID: {connect_id}; access code: {current_code}"
                        else:
                            start_line = f"remote assistance ID: {connect_id}; confirmation mode"
                            if auto_approve:
                                start_line += " (auto approve)"
                        self._safe_report(start_line)
                        await self.events.emit(
                            RoomPublished(connect_id, current_code if code_mode else "", peer_key, mode)
                        )
                        negotiate_task = asyncio.create_task(session.negotiate(), name="uu-tunnel-guest-negotiate")
                        worker_task = asyncio.create_task(
                            workflow._guest_request_worker(
                                requests,
                                pending_password_ids,
                                canceled_confirmation_ids,
                                confirmation_cancel_events,
                                guest_client,
                                session,
                                current_code,
                                approval_callback,
                                auto_approve,
                                decision_state,
                                stop_event,
                                self._report,
                            ),
                            name="uu-tunnel-guest-requests",
                        )
                        track_room_task(negotiate_task)
                        track_room_task(worker_task)
                        channel = await _await_guest_room_phase(
                            workflow._await_guest_negotiation(negotiate_task, worker_task), overload_task
                        )
                        await self._accept_inbound(peer_key, session, channel, via="guest")
                        # 房间已被这个控制端消费；隧道独立运行，下一轮发布新房间。
                        session = None
                        accepted = True
            except workflow._WorkflowStopped:
                stopped = True
            except UuAuthExpiredError:
                raise
            except UuApiCommittedResponseError:
                # 每轮都是全新的游客客户端与房间，结果未知也可安全重建。
                retry = True
                self._safe_report("guest room operation committed response unavailable; recreating guest room")
            except UuApiUnknownOutcomeError:
                retry = True
                self._safe_report("guest room operation outcome unknown; recreating guest room")
            except workflow._GuestRequestCanceled:
                retry = True
                self._safe_report("guest assistance request canceled; recreating guest room")
            except UuApiError:
                retry = True
                self._safe_report("guest room API failure; recreating guest room")
            except _ControlModeBudgetExceeded as exc:
                self._safe_report(str(exc))
                await self.events.emit(TunnelFailed(peer_key, str(exc)))
                raise
            except TunnelSessionError as exc:
                # 单个对端的协商/握手错误只淘汰这次会话，不能拖垮整个协助服务。
                retry = True
                self._safe_report(f"guest tunnel failed: {exc}; recreating guest room")
                await self.events.emit(TunnelFailed(peer_key, str(exc)))
            finally:
                room_closing = True
                for event in confirmation_cancel_events.values():
                    event.set()
                confirmation_cancel_events.clear()
                while True:
                    try:
                        request = requests.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    pending_password_ids.discard(request.control_id)
                if session is not None:
                    session.cancel_authorization_decision()
                try:
                    await workflow._await_owner_shielded(_cancel_and_drain_room_tasks(room_tasks))
                finally:
                    try:
                        if session is not None:
                            await workflow._stop_session_owner_shielded(session)
                    finally:
                        await workflow._await_owner_shielded(guest_client.close())
            if stopped or stop_event.is_set():
                return
            if accepted:
                failure_count = 0
            if retry:
                self._safe_report("retrying guest room after backoff")
                if await workflow._wait_stop(
                    stop_event, workflow._full_jitter_delay(failure_count, cap_s=workflow.ROOM_RETRY_CAP_S)
                ):
                    return
                failure_count += 1

    async def _accept_inbound(self, service_key: str, session: TunnelSession, channel: TunnelChannel, *, via: str) -> None:
        """把刚协商好的入站会话变成一条独立隧道；hello 失败时抛错由服务循环记录。"""

        self._inbound_seq += 1
        peer_key = f"{service_key}#{self._inbound_seq}"
        await self._start_tunnel(peer_key, session, channel, role="controlled", via=via, service=service_key)

    # ---- 隧道生命周期 ----

    async def _start_tunnel(
        self,
        peer_key: str,
        session: TunnelSession,
        channel: TunnelChannel,
        *,
        role: str,
        via: str,
        service: str = "",
    ) -> TunnelInfo:
        loop = asyncio.get_running_loop()
        async with self._lock:
            if not self._accepting or peer_key in self._tunnels:
                await workflow._stop_session_owner_shielded(session)
                if not self._accepting:
                    raise TunnelSessionError("tunnel manager is stopping")
                raise TunnelSessionError(f"{peer_key} 已存在")
            tunnel = _Tunnel(peer_key, role, via, service, session, channel, loop.create_future())
            self._register_forward_handler(session)
            tunnel.task = asyncio.create_task(self._run_tunnel(tunnel), name=f"uu-tunnel-{peer_key}")
            tunnel.task.add_done_callback(_consume_task_result)
            self._tunnels[peer_key] = tunnel
        try:
            return await asyncio.shield(tunnel.ready)
        except asyncio.CancelledError:
            await self.disconnect(peer_key)
            raise

    async def _run_tunnel(self, tunnel: _Tunnel) -> None:
        failure: BaseException | None = None
        reason = "tunnel closed"
        try:
            try:
                tunnel.ctl = await tunnel.session.wait_control_channel(timeout=CONTROL_CHANNEL_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise TunnelProtocolMismatch("隧道握手失败：控制通道未打开") from exc
            tunnel.peer_ip, tunnel.peer_name, tunnel.tun_peer = await self._exchange_hello(tunnel.ctl)
            if tunnel.stop_event.is_set():
                raise workflow._WorkflowStopped()
            async with self._lock:
                tunnel.state = _TunnelState.CONNECTED
            self._attach_route(tunnel)
            tunnel.reader = asyncio.create_task(self._pump_packets(tunnel), name=f"uu-tunnel-packets-{tunnel.peer_key}")
            tunnel.control = asyncio.create_task(self._pump_control(tunnel), name=f"uu-tunnel-control-{tunnel.peer_key}")
            info = self._info(tunnel)
            if not tunnel.ready.done():
                tunnel.ready.set_result(info)
            await self.events.emit(TunnelEstablished(info))
            reason = await self._wait_tunnel_end(tunnel)
        except BaseException as exc:
            failure = exc
        finally:
            cleanup = asyncio.create_task(
                self._cleanup_tunnel(tunnel, failure, reason), name=f"uu-tunnel-cleanup-{tunnel.peer_key}"
            )
            await _await_cleanup(cleanup)

    async def _wait_tunnel_end(self, tunnel: _Tunnel) -> str:
        """等到会话关闭或本地要求停止；迁移替换会话时继续等新会话。"""

        stop_wait = asyncio.create_task(tunnel.stop_event.wait(), name=f"uu-tunnel-stop-{tunnel.peer_key}")
        try:
            while True:
                session = tunnel.session
                generation = tunnel.generation
                session_wait = asyncio.create_task(session.wait_closed())
                try:
                    done, _pending = await asyncio.wait((session_wait, stop_wait), return_when=asyncio.FIRST_COMPLETED)
                    if stop_wait in done:
                        return "disconnected"
                    error = session_wait.result()
                finally:
                    if not session_wait.done():
                        session_wait.cancel()
                        await asyncio.gather(session_wait, return_exceptions=True)
                if await self._transport_replaced(tunnel, generation, stop_wait):
                    continue
                if tunnel.stop_event.is_set():
                    return "disconnected"
                if isinstance(error, TunnelRestartRequired):
                    return "peer disconnected"
                raise error if isinstance(error, Exception) else TunnelSessionError("会话已关闭")
        finally:
            if not stop_wait.done():
                stop_wait.cancel()
                await asyncio.gather(stop_wait, return_exceptions=True)

    async def _cleanup_tunnel(self, tunnel: _Tunnel, failure: BaseException | None, reason: str) -> None:
        async with self._lock:
            established = tunnel.state is _TunnelState.CONNECTED
            tunnel.state = _TunnelState.CLOSING
        await self._close_peer_forwards(tunnel.peer_key)
        self._detach_route(tunnel)
        for task in (tunnel.reader, tunnel.control):
            if task is not None:
                await _cancel_task(task)
        try:
            await workflow._stop_session_owner_shielded(tunnel.session)
        except Exception as exc:
            failure = failure or exc
        async with self._lock:
            if self._tunnels.get(tunnel.peer_key) is tunnel:
                self._tunnels.pop(tunnel.peer_key, None)
        if not tunnel.ready.done():
            if isinstance(failure, asyncio.CancelledError):
                tunnel.ready.cancel()
            elif isinstance(failure, workflow._WorkflowStopped):
                tunnel.ready.set_exception(TunnelSessionError("连接已取消"))
            elif isinstance(failure, Exception):
                tunnel.ready.set_exception(failure)
            else:
                tunnel.ready.set_exception(TunnelSessionError("tunnel setup was interrupted"))
        if not established:
            # 未建成的隧道由调用方（connect 操作 / 服务循环）报错，不重复发事件。
            return
        if failure is None or isinstance(failure, (asyncio.CancelledError, workflow._WorkflowStopped)):
            await self.events.emit(TunnelClosed(tunnel.peer_key, reason if failure is None else "disconnected"))
        else:
            await self.events.emit(TunnelFailed(tunnel.peer_key, _exception_text(failure)))

    async def _exchange_hello(self, ctl: TunnelChannel) -> tuple[IPv4Address, str, bool]:
        """在控制通道上互发 hello，返回对端 (地址, 名称, TUN 已挂载)。"""

        try:
            ctl.send_text(
                control_proto.encode(control_proto.hello(str(self.local_ip), self._hello_name(), self._tun.is_running))
            )
            payload = await asyncio.wait_for(ctl.recv(), timeout=HELLO_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise TunnelProtocolMismatch("隧道握手失败：对端未交换 hello") from exc
        try:
            message = control_proto.decode(payload) if isinstance(payload, str) else None
            if message is None or message["type"] != control_proto.HELLO:
                raise ValueError("not hello")
            return parse_tunnel_address(message["addr"]), str(message["name"]), bool(message["tun"])
        except (ValueError, TunnelConfigError) as exc:
            raise TunnelProtocolMismatch("隧道握手失败：对端 hello 无效") from exc

    def _hello_name(self) -> str:
        name = ""
        if self.client is not None:
            name = str(self.client.state.get("device_name") or "")
        if not name:
            try:
                name = socket.gethostname()
            except OSError:
                name = ""
        name = "".join(character for character in name if character.isprintable())
        return name[: control_proto.MAX_NAME_LENGTH]

    async def _pump_packets(self, tunnel: _Tunnel) -> None:
        """一直读数据通道：TUN 装了该隧道的路由就写入网卡，否则丢弃。通道不读会溢出终止会话。"""

        while True:
            channel = tunnel.channel
            generation = tunnel.generation
            try:
                packet = await channel.recv()
            except asyncio.CancelledError:
                raise
            except Exception:
                if await self._transport_replaced(tunnel, generation):
                    continue
                return
            if not isinstance(packet, bytes):
                continue
            route = tunnel.route
            if route is None:
                continue
            await route.deliver(packet)

    async def _pump_control(self, tunnel: _Tunnel) -> None:
        while True:
            ctl = tunnel.ctl
            generation = tunnel.generation
            if ctl is None:
                return
            try:
                payload = await ctl.recv()
            except asyncio.CancelledError:
                raise
            except Exception:
                if await self._transport_replaced(tunnel, generation):
                    continue
                return
            if not isinstance(payload, str):
                continue
            try:
                message = control_proto.decode(payload)
            except ValueError:
                continue
            kind = message["type"]
            if kind == control_proto.HANDOFF_REQUEST:
                if tunnel.role == "controlled":
                    await self._serve_handoff(tunnel, str(message["request_id"]))
            elif kind in control_proto.HANDOFF_TYPES:
                if not tunnel.handoff_idle.is_set():
                    tunnel.inbox.put_nowait(message)
            else:
                await self._apply_control(tunnel, message)

    async def _apply_control(self, tunnel: _Tunnel, message: dict[str, Any]) -> None:
        """处理对端的状态类控制消息（目前只有 TUN 挂载状态）。"""

        if message["type"] == control_proto.TUN_STATE:
            tunnel.tun_peer = bool(message["mounted"])
            if tunnel.route is not None:
                tunnel.route.enabled = tunnel.tun_peer
            await self.events.emit(TunnelUpdated(self._info(tunnel)))

    async def _drain_channel(self, tunnel: _Tunnel, channel: TunnelChannel) -> None:
        """迁移窗口内替尚未切换成现行传输的新数据通道收包。"""

        while True:
            try:
                packet = await channel.recv()
            except asyncio.CancelledError:
                raise
            except Exception:
                return
            route = tunnel.route
            if isinstance(packet, bytes) and route is not None:
                await route.deliver(packet)

    async def _transport_replaced(
        self, tunnel: _Tunnel, generation: int, stop_wait: asyncio.Task[Any] | None = None
    ) -> bool:
        """旧传输失效时判断是否已被迁移替换；迁移进行中则等它收尾再看代际。"""

        if tunnel.generation != generation:
            return True
        if tunnel.handoff_idle.is_set():
            return False
        idle_wait = asyncio.create_task(tunnel.handoff_idle.wait())
        waiters = (idle_wait, stop_wait) if stop_wait is not None else (idle_wait,)
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not idle_wait.done():
                idle_wait.cancel()
                await asyncio.gather(idle_wait, return_exceptions=True)
        return tunnel.generation != generation

    def _info(self, tunnel: _Tunnel) -> TunnelInfo:
        return TunnelInfo(
            peer_key=tunnel.peer_key,
            role=tunnel.role,
            via=tunnel.via,
            service=tunnel.service,
            local_ip=str(self.local_ip),
            peer_ip=str(tunnel.peer_ip) if tunnel.peer_ip is not None else "",
            peer_name=tunnel.peer_name,
            tun_local=tunnel.route is not None,
            tun_peer=tunnel.tun_peer,
            tun_error=tunnel.tun_error,
            status=tunnel.state.value,
        )

    # ---- 会话准备（控制端）----

    async def _prepare_device(
        self, device_id: str, *, force: bool, force_relay: bool, stop_event: asyncio.Event
    ) -> tuple[TunnelSession, TunnelChannel]:
        client = self._require_client()
        client.require_account_identity()
        control_device_id = await workflow._own_controller_device_id(client, stop_event)
        if control_device_id is None:
            raise workflow._WorkflowStopped()
        join_task = asyncio.create_task(self._join_device(device_id, force))
        upstream = await workflow._await_api_task(join_task, stop_event, commit_sensitive=self._join_factory is None)
        if upstream is None or stop_event.is_set():
            raise workflow._WorkflowStopped()
        return await self._open_controller_session(
            self._room_config(upstream), force_relay=force_relay, control_device_id=control_device_id, assistance=False, stop_event=stop_event
        )

    async def _prepare_assist_by_code(
        self, connect_id: str, access_code: str, *, force_relay: bool, stop_event: asyncio.Event
    ) -> tuple[TunnelSession, TunnelChannel]:
        client = self._require_client()
        client.require_account_identity()
        mode = await workflow._await_api_task(asyncio.create_task(client.get_room_control_mode(connect_id)), stop_event)
        if mode is None or stop_event.is_set():
            raise workflow._WorkflowStopped()
        if mode == CONNECT_TYPE_BY_CONFIRMATION:
            raise UuApiError("该设备需要确认授权（无需密码），请改用不带 --code 的 assist")
        if mode not in (CONNECT_TYPE_BY_PASSWORD, CONNECT_TYPE_PASSWORD_CONFIRMATION):
            raise UuApiError("对端设备未返回可识别的验证方式")
        control_device_id = await workflow._own_controller_device_id(client, stop_event)
        if control_device_id is None:
            raise workflow._WorkflowStopped()
        upstream = await workflow._await_api_task(
            asyncio.create_task(client.join_guest_by_code(connect_id, access_code)), stop_event, commit_sensitive=True
        )
        if upstream is None or stop_event.is_set():
            raise workflow._WorkflowStopped()
        return await self._open_controller_session(
            workflow._require_room_config(upstream),
            force_relay=force_relay,
            control_device_id=control_device_id,
            assistance=True,
            stop_event=stop_event,
        )

    async def _prepare_assist_by_confirmation(
        self, connect_id: str, *, force_relay: bool, stop_event: asyncio.Event
    ) -> tuple[TunnelSession, TunnelChannel]:
        client = self._require_client()
        client.require_account_identity()
        mode = await workflow._await_api_task(asyncio.create_task(client.get_room_control_mode(connect_id)), stop_event)
        if mode is None or stop_event.is_set():
            raise workflow._WorkflowStopped()
        if mode == CONNECT_TYPE_BY_PASSWORD:
            raise UuApiError(f"该设备需要访问码，请使用 assist {connect_id} --code <码>")
        if mode not in workflow._CONFIRMATION_CONTROL_MODES:
            raise UuApiError("对端设备未返回可识别的验证方式")
        control_device_id = await workflow._own_controller_device_id(client, stop_event)
        if control_device_id is None:
            raise workflow._WorkflowStopped()
        self._safe_report(f"waiting for assistance confirmation {connect_id}")
        upstream = await self._wait_assist_confirmation(client, connect_id, stop_event)
        return await self._open_controller_session(
            workflow._require_room_config(upstream),
            force_relay=force_relay,
            control_device_id=control_device_id,
            assistance=True,
            stop_event=stop_event,
        )

    async def _wait_assist_confirmation(
        self, client: UuApiClient, connect_id: str, stop_event: asyncio.Event
    ) -> dict[str, Any]:
        """轮询直到对方确认，返回带 room_config 的原始响应。"""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + ASSIST_CONFIRMATION_TIMEOUT_S
        transient_failures = 0
        while not stop_event.is_set():
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TunnelSessionError("对方未在限定时间内确认")
            try:
                upstream = await workflow._await_api_task(
                    asyncio.create_task(client.poll_assist(connect_id, raw=True)), stop_event
                )
            except UuAuthExpiredError:
                raise
            except UuApiTransientError:
                self._safe_report("assistance confirmation poll transient failure; retrying")
                if await workflow._wait_stop(
                    stop_event,
                    min(remaining, workflow._full_jitter_delay(transient_failures, cap_s=workflow.ROOM_RETRY_CAP_S)),
                ):
                    raise workflow._WorkflowStopped()
                transient_failures += 1
                continue
            if upstream is None:
                raise workflow._WorkflowStopped()
            code = upstream.get("code")
            if code == 0:
                return upstream
            if code != workflow._PENDING_ASSIST_CODE:
                raise UuApiError(f"远程协助失败 code={code}")
            transient_failures = 0
            if await workflow._wait_stop(stop_event, min(remaining, workflow.ASSIST_POLL_INTERVAL_S)):
                raise workflow._WorkflowStopped()
        raise workflow._WorkflowStopped()

    async def _open_controller_session(
        self,
        room_config: Any,
        *,
        force_relay: bool,
        control_device_id: str,
        assistance: bool,
        stop_event: asyncio.Event,
    ) -> tuple[TunnelSession, TunnelChannel]:
        session = self._session_factory(
            room_config,
            role="controller",
            force_relay=force_relay,
            control_device_id=control_device_id,
            assistance=assistance,
        )
        try:
            channel = await workflow._await_session_task(
                session, asyncio.create_task(session.start()), stop_event, stop_session_on_abort=False
            )
            return session, channel
        except BaseException:
            await workflow._stop_session_owner_shielded(session)
            raise

    async def _join_device(self, device_id: str, force: bool) -> Any:
        if self._join_factory is not None:
            return await self._join_factory(device_id, force)
        return await self._require_client().join_room_by_device(device_id, force_join=force)

    def _room_config(self, upstream: Any) -> Any:
        if isinstance(upstream, RoomConfig):
            return upstream
        if isinstance(upstream, dict):
            return workflow._require_room_config(upstream)
        if self._join_factory is not None:
            return upstream
        raise UuApiError("响应中缺少 room_config")

    async def _create_handoff_guest_room(self) -> _HandoffGuestRoom:
        """创建仅接受一次访问码加入的迁移 guest 房。"""

        client = self._guest_client_factory()
        auxiliary: set[asyncio.Task[Any]] = set()
        requests: asyncio.Queue[Any] = asyncio.Queue(maxsize=workflow._GUEST_REQUEST_QUEUE_SIZE)
        pending_password_ids: set[str] = set()
        canceled_ids: set[str] = set()
        cancel_events: dict[str, asyncio.Event] = {}
        decision_state = workflow._GuestDecisionState()
        access_code = workflow.generate_access_code()
        control_stop = asyncio.Event()

        def track_auxiliary(task: asyncio.Task[Any]) -> None:
            auxiliary.add(task)
            task.add_done_callback(auxiliary.discard)
            task.add_done_callback(_consume_task_result)

        control_modes = _ControlModeResponder(client, CONNECT_TYPE_BY_PASSWORD, control_stop, self._report, track_auxiliary)
        overload_task = asyncio.create_task(
            _wait_control_mode_overload(control_modes.overloaded), name="uu-tunnel-handoff-control-mode-budget"
        )
        track_auxiliary(overload_task)

        def on_password(control_id: str, salt: str) -> None:
            if not control_id or not salt or control_id in pending_password_ids:
                return
            try:
                requests.put_nowait(workflow._GuestAccessRequest(workflow._GUEST_REQUEST_PASSWORD, control_id, salt))
            except asyncio.QueueFull:
                return
            pending_password_ids.add(control_id)

        session: TunnelSession | None = None
        try:
            upstream = await client.create_guest_room()
            room_config = workflow._require_room_config(upstream)
            session = self._session_factory(
                room_config,
                role="controlled",
                assist_password_cb=on_password,
                assist_request_cb=lambda _control_id, _name: None,
                assist_cancel_cb=lambda control_id: canceled_ids.add(control_id),
                assist_control_mode_cb=control_modes.handle,
                authorized=False,
            )
            await session.connect_signal()
            connect_id = await client.share_guest_room()

            async def join_once() -> tuple[TunnelSession, TunnelChannel]:
                stop = asyncio.Event()
                negotiate = asyncio.create_task(session.negotiate())

                async def reject(_name: str, _control_id: str) -> bool:
                    return False

                worker = asyncio.create_task(
                    workflow._guest_request_worker(
                        requests,
                        pending_password_ids,
                        canceled_ids,
                        cancel_events,
                        client,
                        session,
                        access_code,
                        reject,
                        False,
                        decision_state,
                        stop,
                        self._report,
                    )
                )
                try:
                    channel = await _await_guest_room_phase(
                        workflow._await_guest_negotiation(negotiate, worker), overload_task
                    )
                    return session, channel
                finally:
                    await workflow._cancel_and_drain_tasks(negotiate, worker)

            join_task = asyncio.create_task(join_once(), name="uu-tunnel-handoff-guest-join")
            return _HandoffGuestRoom(connect_id, access_code, session, client, join_task, auxiliary)
        except BaseException:
            control_stop.set()
            await workflow._cancel_and_drain_tasks(*tuple(auxiliary))
            if session is not None:
                await workflow._stop_session_owner_shielded(session)
            await client.close()
            raise

    # ---- 收尾 ----

    async def _stop_once(self) -> None:
        async with self._lock:
            self._accepting = False
            services = tuple(self._services.values())
            for service in services:
                service.stop_event.set()
                if not service.task.done():
                    service.task.cancel()
        if services:
            await asyncio.gather(*(_drain_task(service.task) for service in services))
        await self.disconnect_all()
        await self._close_all_forwards()
        for task in tuple(self._background):
            await _cancel_task(task)
        async with self._tun_lock:
            if self._tun_watch is not None:
                await _cancel_task(self._tun_watch)
                self._tun_watch = None
            await self._tun.stop()

    def _register_forward_handler(self, session: TunnelSession) -> None:
        """两端都接受对端发起的端口映射通道。"""

        register = getattr(session, "set_dynamic_channel_handler", None)
        if callable(register):
            register("FWD/", self._forward_acceptor.accept)

    async def _close_peer_forwards(self, peer_key: str) -> None:
        async with self._forward_lock:
            listeners = [listener for listener in self._forwards.values() if listener.spec.peer_key == peer_key]
            for listener in listeners:
                self._forwards.pop(listener.spec.forward_id, None)
        if listeners:
            await asyncio.gather(*(listener.close() for listener in listeners), return_exceptions=True)

    async def _close_all_forwards(self) -> None:
        async with self._forward_lock:
            listeners = tuple(self._forwards.values())
            self._forwards.clear()
        if listeners:
            await asyncio.gather(*(listener.close() for listener in listeners), return_exceptions=True)

    def _require_client(self) -> UuApiClient:
        if self.client is None:
            raise UuApiError("account client is required")
        return self.client

    def _safe_report(self, message: str) -> None:
        try:
            self._report(message)
        except Exception:
            logger.warning("manager report callback failed", exc_info=True)


async def _inbox_message(tunnel: _Tunnel, request_id: str, allowed: set[str]) -> dict[str, Any]:
    while True:
        message = await tunnel.inbox.get()
        if message.get("request_id") != request_id:
            continue
        if message["type"] not in allowed:
            raise TunnelSessionError("迁移响应类型无效")
        return message


async def _recv_handoff(
    channel: TunnelChannel,
    request_id: str,
    allowed: set[str],
    *,
    on_other: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """在尚无控制泵的新通道上直接等待匹配的迁移消息；其他状态消息交给 on_other。"""

    while True:
        payload = await channel.recv()
        if not isinstance(payload, str):
            raise TunnelSessionError("迁移控制消息必须是文本")
        try:
            message = control_proto.decode(payload)
        except ValueError as exc:
            raise TunnelSessionError("迁移控制消息无效") from exc
        if message["type"] not in control_proto.HANDOFF_TYPES:
            if on_other is not None:
                await on_other(message)
            continue
        if message.get("request_id") != request_id:
            continue
        if message["type"] not in allowed:
            raise TunnelSessionError("迁移响应类型无效")
        return message


def _set_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        return


async def _wait_control_mode_overload(event: asyncio.Event) -> None:
    await event.wait()
    raise _ControlModeBudgetExceeded("guest control-mode ID budget exceeded; service stopped")


async def _await_guest_room_phase(awaitable: Awaitable[Any], overload_task: asyncio.Task[None]) -> Any:
    """等待房间阶段，同时让 control-mode 异常负载立即终止该阶段。"""

    operation = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait((operation, overload_task), return_when=asyncio.FIRST_COMPLETED)
        if overload_task in done:
            await overload_task
        return await operation
    finally:
        if not operation.done():
            await workflow._cancel_and_drain_task(operation)


async def _cancel_and_drain_room_tasks(tasks: set[asyncio.Task[Any]]) -> None:
    while tasks:
        snapshot = tuple(tasks)
        await workflow._cancel_and_drain_tasks(*snapshot)
        tasks.difference_update(snapshot)


async def _cancel_task(task: asyncio.Task[Any]) -> None:
    if task is asyncio.current_task():
        return
    if not task.done():
        task.cancel()
    await _drain_task(task)


async def _drain_task(task: asyncio.Task[Any]) -> None:
    try:
        await task
    except asyncio.CancelledError:
        return
    except Exception:
        logger.warning("manager-owned task failed during cleanup", exc_info=True)


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


def _exception_text(error: BaseException) -> str:
    text = str(error).strip()
    if not text and isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return "操作超时"
    return text or type(error).__name__


__all__ = ["TunnelManager", "TunnelProtocolMismatch"]

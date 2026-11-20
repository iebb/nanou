"""核心运行时的类型化事件。"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal, TypeAlias


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TunnelInfo:
    """一条隧道的对外快照。"""

    peer_key: str
    role: str  # controller | controlled
    via: str  # account | guest
    service: str  # 入站隧道所属服务键；出站为空
    local_ip: str
    peer_ip: str  # 对端公告的隧道地址
    peer_name: str
    tun_local: bool  # 本机 TUN 已为该隧道装路由
    tun_peer: bool  # 对端 TUN 已挂载
    tun_error: str  # 本机无法为该隧道装路由的原因（地址冲突等）
    status: str = "connected"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TunnelEstablished:
    """一条隧道完成 hello 交换，进入连接态。"""

    info: TunnelInfo


@dataclass(frozen=True, slots=True)
class TunnelUpdated:
    """隧道的 TUN/来源等属性变化（挂载状态、对端状态、迁移）。"""

    info: TunnelInfo


@dataclass(frozen=True, slots=True)
class TunnelFailed:
    peer_key: str
    error: str


@dataclass(frozen=True, slots=True)
class TunnelClosed:
    peer_key: str
    reason: str


@dataclass(frozen=True, slots=True)
class TunFatal:
    """本机 TUN 泵故障，网卡已视为卸载；隧道不受影响。"""

    error: str


@dataclass(frozen=True, slots=True)
class RoomPublished:
    """协助房间已发布；mode 为 code（凭访问码加入）或 confirm（由本机确认加入）。"""

    connect_id: str
    access_code: str
    owner_key: str
    mode: str = "code"


GUEST_MODE_CODE = "code"
GUEST_MODE_CONFIRM = "confirm"
GUEST_MODES = frozenset({GUEST_MODE_CODE, GUEST_MODE_CONFIRM})


HandoffStage: TypeAlias = Literal[
    "requested",
    "room_ready",
    "prepared",
    "committed",
    "completed",
    "failed",
]


@dataclass(frozen=True, slots=True)
class HandoffState:
    """迁移阶段，仅取 HandoffStage 定义的六个值。"""

    peer_key: str
    stage: HandoffStage
    reason: str = ""


CoreEvent: TypeAlias = (
    TunnelEstablished
    | TunnelUpdated
    | TunnelFailed
    | TunnelClosed
    | TunFatal
    | RoomPublished
    | HandoffState
)
EventCallback: TypeAlias = Callable[[CoreEvent], Awaitable[None] | None]


class EventBus:
    """按订阅顺序发布事件，并隔离单个监听器故障。"""

    def __init__(self) -> None:
        self._callbacks: list[EventCallback] = []

    def subscribe(self, callback: EventCallback) -> EventCallback:
        if not callable(callback):
            raise TypeError("event callback must be callable")
        if callback not in self._callbacks:
            self._callbacks.append(callback)
        return callback

    def unsubscribe(self, callback: EventCallback) -> None:
        try:
            self._callbacks.remove(callback)
        except ValueError:
            return

    async def emit(self, event: CoreEvent) -> None:
        for callback in tuple(self._callbacks):
            try:
                result: Any = callback(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.warning("event listener failed", exc_info=True)


__all__ = [
    "GUEST_MODES",
    "GUEST_MODE_CODE",
    "GUEST_MODE_CONFIRM",
    "CoreEvent",
    "EventBus",
    "EventCallback",
    "HandoffStage",
    "HandoffState",
    "RoomPublished",
    "TunFatal",
    "TunnelClosed",
    "TunnelEstablished",
    "TunnelFailed",
    "TunnelInfo",
    "TunnelUpdated",
]

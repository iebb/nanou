"""隧道核心：编排、事件与端口映射。"""

from ..tun.config import TunnelError
from ..webrtc import TunnelSessionError
from .events import (
    GUEST_MODES,
    GUEST_MODE_CODE,
    GUEST_MODE_CONFIRM,
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
from .manager import TunnelManager, TunnelProtocolMismatch

__all__ = [
    "EventBus",
    "ForwardAcceptor",
    "ForwardListener",
    "ForwardSpec",
    "GUEST_MODES",
    "GUEST_MODE_CODE",
    "GUEST_MODE_CONFIRM",
    "HandoffState",
    "RoomPublished",
    "TunFatal",
    "TunnelClosed",
    "TunnelError",
    "TunnelEstablished",
    "TunnelFailed",
    "TunnelInfo",
    "TunnelManager",
    "TunnelProtocolMismatch",
    "TunnelSessionError",
    "TunnelUpdated",
]

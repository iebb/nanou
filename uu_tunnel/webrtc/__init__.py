"""WebRTC 层：aiortc 封装 + UU 信令事件流驱动协商。"""

from .channel import TunnelChannel
from .ice_bootstrap import IceAnswerStatus
from .session import (
    TUNNEL_CTL_CHANNEL_LABEL,
    TUNNEL_CHANNEL_LABEL,
    TunnelControlOutcomeUnknown,
    TunnelIceProfilesExhausted,
    TunnelNegotiationRetryableError,
    TunnelProtocolError,
    TunnelRestartRequired,
    TunnelSession,
    TunnelSessionError,
    TunnelSoacOutcomeUnknown,
)

__all__ = [
    "TunnelChannel",
    "TUNNEL_CHANNEL_LABEL",
    "TUNNEL_CTL_CHANNEL_LABEL",
    "IceAnswerStatus",
    "TunnelSession",
    "TunnelSessionError",
    "TunnelControlOutcomeUnknown",
    "TunnelSoacOutcomeUnknown",
    "TunnelNegotiationRetryableError",
    "TunnelProtocolError",
    "TunnelIceProfilesExhausted",
    "TunnelRestartRequired",
]

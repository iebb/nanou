"""信令层：基于 python-socketio 的 Socket.IO v5 客户端。"""

from .client import (
    SignalCallbacks,
    SignalConnectionError,
    SocketIoSignalClient,
)

__all__ = [
    "SocketIoSignalClient",
    "SignalCallbacks",
    "SignalConnectionError",
]

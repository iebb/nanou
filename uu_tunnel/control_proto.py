"""控制通道（ctl DataChannel）的有界 JSON 消息。

隧道建立后两端各发一条 ``hello`` 公告本机隧道地址、名称与 TUN 状态；之后 TUN 挂载/卸载时
发 ``tun`` 通知对端。handoff 五类消息用于把一条隧道的传输迁移到一次性协助会话。
"""

from __future__ import annotations

import json
from typing import Any

from .tun.config import parse_tunnel_address
from .webrtc import TUNNEL_CTL_CHANNEL_LABEL
from .webrtc.channel import MAX_TEXT_BYTES


CONTROL_CHANNEL_LABEL = TUNNEL_CTL_CHANNEL_LABEL
VERSION = 2
HELLO = "hello"
TUN_STATE = "tun"
HANDOFF_REQUEST = "handoff_request"
HANDOFF_ROOM = "handoff_room"
HANDOFF_ERROR = "handoff_error"
HANDOFF_COMMIT = "handoff_commit"
HANDOFF_COMMITTED = "handoff_committed"
HANDOFF_TYPES = frozenset(
    {HANDOFF_REQUEST, HANDOFF_ROOM, HANDOFF_ERROR, HANDOFF_COMMIT, HANDOFF_COMMITTED}
)
MAX_NAME_LENGTH = 64


def encode(message: dict[str, Any]) -> str:
    """编码一条已校验的控制消息。"""

    normalized = _validate(message)
    try:
        text = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        size = len(text.encode("utf-8"))
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("control message is invalid") from exc
    if size > MAX_TEXT_BYTES:
        raise ValueError("control message exceeds the text limit")
    return text


def decode(text: str) -> dict[str, Any]:
    """解码并校验一条控制消息。"""

    if not isinstance(text, str):
        raise ValueError("control message must be text")
    try:
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("control message exceeds the text limit")
        value = json.loads(text)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("control message is invalid") from exc
    return _validate(value)


def hello(address: str, name: str, tun_mounted: bool) -> dict[str, Any]:
    return _validate({"type": HELLO, "v": VERSION, "addr": address, "name": name, "tun": tun_mounted})


def tun_state(mounted: bool) -> dict[str, Any]:
    return _validate({"type": TUN_STATE, "v": VERSION, "mounted": mounted})


def handoff_request(request_id: str) -> dict[str, Any]:
    return _validate({"type": HANDOFF_REQUEST, "v": VERSION, "request_id": request_id})


def handoff_room(request_id: str, connect_id: str, access_code: str) -> dict[str, Any]:
    return _validate(
        {
            "type": HANDOFF_ROOM,
            "v": VERSION,
            "request_id": request_id,
            "connect_id": connect_id,
            "access_code": access_code,
        }
    )


def handoff_error(request_id: str, reason: str) -> dict[str, Any]:
    return _validate({"type": HANDOFF_ERROR, "v": VERSION, "request_id": request_id, "reason": reason[:512]})


def handoff_commit(request_id: str) -> dict[str, Any]:
    return _validate({"type": HANDOFF_COMMIT, "v": VERSION, "request_id": request_id})


def handoff_committed(request_id: str) -> dict[str, Any]:
    return _validate({"type": HANDOFF_COMMITTED, "v": VERSION, "request_id": request_id})


def _validate(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("control message must be an object")
    message_type = value.get("type")
    if not isinstance(message_type, str) or not message_type or message_type != message_type.strip():
        raise ValueError("control message type is invalid")
    if value.get("v") != VERSION:
        raise ValueError("control message version is unsupported")
    if message_type == HELLO:
        _exact_fields(value, {"type", "v", "addr", "name", "tun"})
        parse_tunnel_address(value.get("addr"))
        name = value.get("name")
        if not isinstance(name, str) or len(name) > MAX_NAME_LENGTH or not name.isprintable():
            raise ValueError("hello name is invalid")
        if type(value.get("tun")) is not bool:
            raise ValueError("hello tun must be a bool")
    elif message_type == TUN_STATE:
        _exact_fields(value, {"type", "v", "mounted"})
        if type(value.get("mounted")) is not bool:
            raise ValueError("tun mounted must be a bool")
    elif message_type == HANDOFF_ROOM:
        _exact_fields(value, {"type", "v", "request_id", "connect_id", "access_code"})
        _bounded_text(value.get("request_id"), "handoff request_id", 64)
        _bounded_text(value.get("connect_id"), "handoff connect_id", 512)
        _bounded_text(value.get("access_code"), "handoff access_code", 64)
    elif message_type == HANDOFF_ERROR:
        _exact_fields(value, {"type", "v", "request_id", "reason"})
        _bounded_text(value.get("request_id"), "handoff request_id", 64)
        _bounded_text(value.get("reason"), "handoff reason", 512)
    elif message_type in {HANDOFF_REQUEST, HANDOFF_COMMIT, HANDOFF_COMMITTED}:
        _exact_fields(value, {"type", "v", "request_id"})
        _bounded_text(value.get("request_id"), "handoff request_id", 64)
    else:
        raise ValueError("control message type is unknown")
    return dict(value)


def _exact_fields(value: dict[str, Any], fields: set[str]) -> None:
    if set(value) != fields:
        raise ValueError("control message fields are invalid")


def _bounded_text(value: object, label: str, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > limit
        or not value.isprintable()
    ):
        raise ValueError(f"{label} is invalid")
    return value


__all__ = [
    "CONTROL_CHANNEL_LABEL",
    "HANDOFF_COMMIT",
    "HANDOFF_COMMITTED",
    "HANDOFF_ERROR",
    "HANDOFF_REQUEST",
    "HANDOFF_ROOM",
    "HANDOFF_TYPES",
    "HELLO",
    "MAX_NAME_LENGTH",
    "TUN_STATE",
    "VERSION",
    "decode",
    "encode",
    "handoff_commit",
    "handoff_committed",
    "handoff_error",
    "handoff_request",
    "handoff_room",
    "hello",
    "tun_state",
]

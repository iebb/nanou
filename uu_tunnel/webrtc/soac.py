"""信令头与 SOAC 载荷构造。

UU 信令协议要点：
  * WebSocket 握手头：X-NRD-AUTH=房间token、X-NRD-CONTROLLING={0|1}、
    streamer_version、streamer_flag={"sdp_flags":{"gzip_sdp":true}}
  * "control" 事件（带 ack）：app_control_id / app_data / streamer_data 三参
  * "soac" 事件（可选 ack）：{client_id, data:{type, sdp, gzip_sdp, ...}}
   * SDP 走 gzip：payload.data.sdp 置空，gzip_sdp 为 ``gzip.compress`` 压缩字节
    （Socket.IO 二进制附件传输）
"""

from __future__ import annotations

import gzip
import json
import zlib
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any

from .ice_bootstrap import EXTENSION_KEY

STREAMER_CLIENT_VERSION = "V3.1.14"
MAX_COMPRESSED_SDP_BYTES = 256 * 1024
MAX_DECOMPRESSED_SDP_BYTES = 1024 * 1024
MAX_CANDIDATE_BYTES = 4096


def build_signal_headers(token: str, controlled: bool) -> dict[str, str]:
    """信令 WebSocket 握手头。

    controlled：False=主控端（进房连接），True=被控端（房间属主）。
    """
    flag = json.dumps({"sdp_flags": {"gzip_sdp": True}}, separators=(",", ":"))
    return {
        "X-NRD-AUTH": token,
        "X-NRD-CONTROLLING": "1" if controlled else "0",
        "streamer_version": STREAMER_CLIENT_VERSION,
        "streamer_flag": flag,
    }


class SoacBuilder:
    """构造 soac 事件载荷。

    类型（signalSoac.ts STREAMER_SOAC_TYPES）：
        offer / answer / restart_ice
    """

    @staticmethod
    def offer(
        client_id: str | None,
        ice_id: str | None,
        app_control_id: str | None,
        sdp: str,
        extension: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return SoacBuilder._wrap(
            client_id,
            SoacBuilder._sdp_data("offer", ice_id, app_control_id, sdp, extension),
        )

    @staticmethod
    def answer(
        client_id: str | None,
        ice_id: str | None,
        app_control_id: str | None,
        sdp: str | None = None,
        extension: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build an SDP answer or a credential-free ICE-status NACK.

        ``sdp=None`` is intentionally represented without ``sdp`` or
        ``gzip_sdp`` fields.  It is used only for the route-bound ICE-answer
        NACK extension, so a local failure never leaks an SDP fragment while
        still preserving the normal successful-answer wire shape.
        """

        return SoacBuilder._wrap(
            client_id,
            SoacBuilder._sdp_data(
                "answer",
                ice_id,
                app_control_id,
                sdp,
                extension,
                allow_empty_sdp=True,
            ),
        )

    @staticmethod
    def candidate(
        client_id: str | None,
        ice_id: str | None,
        app_control_id: str | None,
        candidate: str | Mapping[str, Any],
        extension: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build a candidate message without inventing network-type fields.

        Existing consumers use a candidate dictionary.  A bare candidate
        string is also retained verbatim for peers that use that SOAC shape.
        Mapping input is copied so later caller mutation cannot alter the
        emitted payload.
        """

        if isinstance(candidate, Mapping):
            copied_candidate: str | dict[str, Any] = dict(candidate)
            _validate_candidate_value(copied_candidate.get("candidate"))
        elif isinstance(candidate, str):
            _validate_candidate_value(candidate)
            copied_candidate = candidate
        else:
            raise TypeError("candidate must be a string or mapping")
        data: dict[str, Any] = {"type": "candidate", "candidate": copied_candidate}
        SoacBuilder._add_route_fields(data, ice_id, app_control_id)
        if extension is not None:
            if not isinstance(extension, Mapping):
                raise TypeError("extension must be a mapping")
            data[EXTENSION_KEY] = dict(extension)
        return SoacBuilder._wrap(client_id, data)

    @staticmethod
    def restart_ice(
        client_id: str | None,
        ice_id: str | None,
        app_control_id: str | None,
        ice_network_type: int,
        extension: Mapping[str, Any] | None = None,
        sdp: str | None = None,
    ) -> dict[str, Any]:
        """Build a restart request or a complete replacement-offer form."""

        if type(ice_network_type) is not int:
            raise TypeError("ice_network_type must be an integer")
        if sdp is not None:
            data = SoacBuilder._sdp_data(
                "restart_ice",
                ice_id,
                app_control_id,
                sdp,
                extension,
            )
            data["ice_network_type"] = ice_network_type
            return SoacBuilder._wrap(client_id, data)
        data: dict[str, Any] = {"type": "restart_ice"}
        SoacBuilder._add_route_fields(data, ice_id, app_control_id)
        data["ice_network_type"] = ice_network_type
        if extension is not None:
            if not isinstance(extension, Mapping):
                raise TypeError("extension must be a mapping")
            data[EXTENSION_KEY] = dict(extension)
        return SoacBuilder._wrap(client_id, data)

    @staticmethod
    def _sdp_data(
        message_type: str,
        ice_id: str | None,
        app_control_id: str | None,
        sdp: str | None,
        extension: Mapping[str, Any] | None,
        *,
        allow_empty_sdp: bool = False,
    ) -> dict[str, Any]:
        if sdp is None:
            if not allow_empty_sdp:
                raise TypeError("sdp must be a string")
            data: dict[str, Any] = {"type": message_type}
            SoacBuilder._add_route_fields(data, ice_id, app_control_id)
            data["ice_network_type"] = 3
            if extension is not None:
                if not isinstance(extension, Mapping):
                    raise TypeError("extension must be a mapping")
                data[EXTENSION_KEY] = dict(extension)
            return data
        if not isinstance(sdp, str):
            raise TypeError("sdp must be a string")
        try:
            encoded_sdp = sdp.encode()
        except UnicodeEncodeError as exc:
            raise ValueError("sdp must be valid UTF-8") from exc
        if len(encoded_sdp) > MAX_DECOMPRESSED_SDP_BYTES:
            raise ValueError("sdp exceeds maximum size")
        compressed_sdp = gzip.compress(encoded_sdp, compresslevel=6)
        if len(compressed_sdp) > MAX_COMPRESSED_SDP_BYTES:
            raise ValueError("compressed sdp exceeds maximum size")
        data: dict[str, Any] = {
            "type": message_type,
            "sdp": "",
            "gzip_sdp": compressed_sdp,  # Socket.IO binary attachment
        }
        SoacBuilder._add_route_fields(data, ice_id, app_control_id)
        data["ice_network_type"] = 3  # appAuto（signalSoac.ts STREAMER_ICE_NETWORK_TYPES）
        if extension is not None:
            if not isinstance(extension, Mapping):
                raise TypeError("extension must be a mapping")
            # A shallow copy is intentional: extension values are protocol
            # values, while the top-level map must not remain caller-owned.
            data[EXTENSION_KEY] = dict(extension)
        return data

    @staticmethod
    def _add_route_fields(
        data: dict[str, Any],
        ice_id: str | None,
        app_control_id: str | None,
    ) -> None:
        if ice_id is not None:
            data["ice_id"] = ice_id
        if app_control_id is not None:
            data["app_control_id"] = app_control_id

    @staticmethod
    def _wrap(client_id: str | None, data: dict[str, Any]) -> dict[str, Any]:
        return {"client_id": client_id, "data": data} if client_id else {"data": data}


@dataclass(frozen=True)
class SoacRoute:
    """Routing fields carried around a SOAC message without changing its data."""

    client_id: str | None = None
    ice_id: str | None = None
    app_control_id: str | None = None


@dataclass(frozen=True)
class SoacMessage:
    """A parsed SOAC payload with its exact nested data dictionary."""

    route: SoacRoute
    data: dict[str, Any]


def parse_soac_data(event_args: list[Any]) -> SoacMessage | None:
    """Parse SOAC routing plus data from wrapper, direct, and legacy argument shapes.

    A wrapper's ``client_id`` always wins over a separate string argument, so
    unrelated Socket.IO arguments cannot replace explicit routing information.
    """
    for index, arg in enumerate(event_args):
        if isinstance(arg, dict):
            data = arg.get("data") if isinstance(arg.get("data"), dict) else None
            if data is not None:
                if not _candidate_data_within_bound(data):
                    return None
                return SoacMessage(_route_from(arg, data, event_args, index), data)
            if arg.get("type") in (
                "offer",
                "answer",
                "candidate",
                "restart_ice",
                "switch_network_notify",
            ):
                if not _candidate_data_within_bound(arg):
                    return None
                return SoacMessage(_route_from(arg, arg, event_args, index), arg)
    return None


def _route_from(
    wrapper: dict[str, Any],
    data: dict[str, Any],
    event_args: list[Any],
    data_index: int,
) -> SoacRoute:
    client_id = _route_string(wrapper.get("client_id"))
    if client_id is None and data_index > 0:
        client_id = _route_string(event_args[data_index - 1])
    return SoacRoute(
        client_id=client_id,
        ice_id=_route_string(data.get("ice_id")),
        app_control_id=_route_string(data.get("app_control_id")),
    )


def _route_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def decode_sdp_from_soac(data: dict[str, Any]) -> str | None:
    """从 soac data 取受限明文 SDP（自动 gunzip gzip_sdp 字段）。"""
    sdp = data.get("sdp")
    if isinstance(sdp, str) and sdp:
        try:
            if len(sdp.encode()) <= MAX_DECOMPRESSED_SDP_BYTES:
                return sdp
        except UnicodeEncodeError:
            return None
        return None
    gzip_sdp = data.get("gzip_sdp")
    if isinstance(gzip_sdp, (bytes, bytearray)):
        if len(gzip_sdp) > MAX_COMPRESSED_SDP_BYTES:
            return None
        return _decode_bounded_gzip_sdp(bytes(gzip_sdp))
    return None


def _decode_bounded_gzip_sdp(payload: bytes) -> str | None:
    """Decode one complete gzip member without allocating past the SDP cap."""

    try:
        decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
        decoded = decompressor.decompress(payload, MAX_DECOMPRESSED_SDP_BYTES + 1)
        if len(decoded) > MAX_DECOMPRESSED_SDP_BYTES or decompressor.unconsumed_tail:
            return None
        flushed = decompressor.flush(MAX_DECOMPRESSED_SDP_BYTES + 1 - len(decoded))
        if len(decoded) + len(flushed) > MAX_DECOMPRESSED_SDP_BYTES:
            return None
        if not decompressor.eof or decompressor.unused_data:
            return None
        return (decoded + flushed).decode()
    except (UnicodeDecodeError, ValueError, zlib.error):
        return None


def _candidate_data_within_bound(data: dict[str, Any]) -> bool:
    if data.get("type") != "candidate":
        return True
    candidate = data.get("candidate")
    if isinstance(candidate, Mapping):
        candidate = candidate.get("candidate")
    return not isinstance(candidate, str) or _candidate_size_within_bound(candidate)


def _validate_candidate_value(candidate: Any) -> None:
    if not isinstance(candidate, str):
        raise TypeError("candidate string is required")
    if not _candidate_size_within_bound(candidate):
        raise ValueError("candidate exceeds maximum size")


def _candidate_size_within_bound(candidate: str) -> bool:
    try:
        return len(candidate.encode()) <= MAX_CANDIDATE_BYTES
    except UnicodeEncodeError:
        return False

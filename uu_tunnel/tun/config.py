"""隧道网段常量、本机隧道地址与原始 IPv4 包校验。"""

from __future__ import annotations

import hashlib
from enum import Enum
from ipaddress import IPv4Address, IPv4Network
from typing import Final


class PacketDropReason(str, Enum):
    """被拒绝 IPv4 包的稳定原因码，不含载荷。"""

    TYPE = "type"
    SIZE = "size"
    VERSION = "version"
    IHL = "ihl"
    TOTAL_LENGTH = "total_length"
    SOURCE = "source"
    DESTINATION = "destination"


class TunnelError(RuntimeError):
    """TUN 层错误基类。"""


class TunnelConfigError(TunnelError, ValueError):
    """地址或固定参数无效。"""


class TunnelPacketError(TunnelConfigError):
    """原始 IPv4 包对本端点无效。"""

    def __init__(self, message: str, *, reason: PacketDropReason) -> None:
        super().__init__(message)
        self.reason = reason


class TunnelPlatformError(TunnelError):
    """当前平台没有可用的原生 TUN。"""


class TunnelStateError(TunnelError):
    """设备操作与其生命周期状态不符。"""


class TunnelDeviceError(TunnelError):
    """原生 TUN 设备操作失败。"""


TUNNEL_SUPERNET: Final[IPv4Network] = IPv4Network("100.64.0.0/10")
TUNNEL_MTU: Final[int] = 1280
_IPV4_HEADER_SIZE: Final[int] = 20


def local_tunnel_address(identity: str) -> IPv4Address:
    """由稳定本机身份派生本机隧道地址：网段内的一个固定主机地址，不区分角色。"""

    if not isinstance(identity, str) or not identity:
        raise TunnelConfigError("machine identity is invalid")
    digest = hashlib.sha256(f"uu-tunnel-address:{identity}".encode("utf-8")).digest()
    hosts = TUNNEL_SUPERNET.num_addresses - 2
    offset = int.from_bytes(digest[:8], "big") % hosts
    return IPv4Address(int(TUNNEL_SUPERNET.network_address) + 1 + offset)


def parse_tunnel_address(value: object) -> IPv4Address:
    """校验对端公告的隧道地址：必须是网段内的主机地址。"""

    if not isinstance(value, str):
        raise TunnelConfigError("tunnel address must be text")
    try:
        address = IPv4Address(value)
    except ValueError as exc:
        raise TunnelConfigError("tunnel address is invalid") from exc
    if address not in TUNNEL_SUPERNET or address in (
        TUNNEL_SUPERNET.network_address,
        TUNNEL_SUPERNET.broadcast_address,
    ):
        raise TunnelConfigError("tunnel address is outside 100.64.0.0/10")
    return address


def validate_packet(
    packet: bytes,
    *,
    mtu: int,
    expected_source: IPv4Address | None = None,
    expected_destination: IPv4Address | None = None,
) -> bytes:
    """校验原始 IPv4 包，可选限制源或目的地址。"""

    if not isinstance(packet, bytes):
        raise TunnelPacketError("IPv4 packet must be bytes", reason=PacketDropReason.TYPE)
    if not _IPV4_HEADER_SIZE <= len(packet) <= mtu:
        raise TunnelPacketError("IPv4 packet size is invalid", reason=PacketDropReason.SIZE)
    if packet[0] >> 4 != 4:
        raise TunnelPacketError(
            "IPv4 packet version is invalid", reason=PacketDropReason.VERSION
        )
    header_length = (packet[0] & 0x0F) * 4
    if not _IPV4_HEADER_SIZE <= header_length <= len(packet):
        raise TunnelPacketError("IPv4 header length is invalid", reason=PacketDropReason.IHL)
    if int.from_bytes(packet[2:4], "big") != len(packet):
        raise TunnelPacketError(
            "IPv4 total length is invalid", reason=PacketDropReason.TOTAL_LENGTH
        )
    if expected_source is not None and IPv4Address(packet[12:16]) != expected_source:
        raise TunnelPacketError("IPv4 packet source is invalid", reason=PacketDropReason.SOURCE)
    if expected_destination is not None and IPv4Address(packet[16:20]) != expected_destination:
        raise TunnelPacketError(
            "IPv4 packet destination is invalid", reason=PacketDropReason.DESTINATION
        )
    return packet


__all__ = [
    "PacketDropReason",
    "TUNNEL_MTU",
    "TUNNEL_SUPERNET",
    "TunnelConfigError",
    "TunnelDeviceError",
    "TunnelError",
    "TunnelPacketError",
    "TunnelPlatformError",
    "TunnelStateError",
    "local_tunnel_address",
    "parse_tunnel_address",
    "validate_packet",
]

"""Bounded extraction of the observed UU room record."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MAX_ROOM_TOKEN_LENGTH = 4096
MAX_SIGNAL_SERVERS = 16
MAX_SIGNAL_SERVER_LENGTH = 253


@dataclass(frozen=True, slots=True)
class RoomConfig:
    """The room token and signal hosts accepted by the signal adapter."""

    token: str
    signal_servers: tuple[str, ...]


def extract_room_config(upstream: dict[str, Any]) -> RoomConfig | None:
    """Extract only observed direct, ``data``, and ``room_config`` wrappers.

    The official room object uses exactly ``token`` and ``signaling_list``.
    Arbitrary recursive wrappers and guessed field aliases are intentionally
    rejected so an unrelated response cannot be mistaken for a room.
    """
    if not isinstance(upstream, dict):
        return None

    data = upstream.get("data")
    room_config = upstream.get("room_config")
    candidates: list[Any] = [upstream, data, room_config]
    if isinstance(data, dict):
        candidates.append(data.get("room_config"))
    for record in candidates:
        parsed = _parse_room_record(record)
        if parsed is not None:
            return parsed
    return None


def _parse_room_record(record: Any) -> RoomConfig | None:
    if not isinstance(record, dict):
        return None
    token = record.get("token")
    servers = record.get("signaling_list")
    if not _bounded_string(token, MAX_ROOM_TOKEN_LENGTH):
        return None
    if not isinstance(servers, list) or not 1 <= len(servers) <= MAX_SIGNAL_SERVERS:
        return None
    if not all(_bounded_string(server, MAX_SIGNAL_SERVER_LENGTH) for server in servers):
        return None
    return RoomConfig(token=token, signal_servers=tuple(dict.fromkeys(servers)))


def _bounded_string(value: Any, max_length: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= max_length

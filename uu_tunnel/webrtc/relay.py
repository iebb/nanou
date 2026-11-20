"""Pinned, per-peer private aiortc/aioice forced-relay integration."""

from __future__ import annotations

import asyncio
from types import MethodType
from typing import Any

import aioice
import aiortc
from aiortc import RTCPeerConnection


AIORTC_VERSION = "1.15.0"
AIOICE_VERSION = "0.10.2+uutun1"
_PREPARE_MARKER = "_uu_tunnel_forced_relay_state"


class RelayError(RuntimeError):
    """Forced relay cannot be safely enforced with the active ICE stack."""


def prepare_forced_relay(peer_connection: RTCPeerConnection) -> None:
    """Install relay-only gathering on this peer connection's ICE gatherer.

    ``RTCPeerConnection.createDataChannel`` must have run first, so SCTP has
    allocated its DTLS/ICE transport.  No class or global method is modified.
    """
    _require_pinned_versions()
    gatherer, connection = _locate_private_connection(peer_connection)
    _require_private_fields(
        connection,
        "_transport_policy",
        "_use_ipv4",
        "_use_ipv6",
        "_protocols",
        "_local_candidates",
        "_nominated",
    )

    existing = getattr(gatherer, _PREPARE_MARKER, None)
    if existing is not None:
        if not isinstance(existing, dict) or existing.get("connection") is not connection:
            raise RelayError("forced relay gatherer state is invalid")
        return

    original_gather = getattr(gatherer, "gather", None)
    if not callable(original_gather):
        raise RelayError("forced relay ICE gatherer has no gather method")

    try:
        connection._transport_policy = aioice.TransportPolicy.RELAY
        # aioice 0.10.2 creates host protocols for every address even with
        # RELAY policy.  Empty address selection skips that loop entirely,
        # while its independent turn_server task still creates the relay
        # protocol and candidate.
        connection._use_ipv4 = False
        connection._use_ipv6 = False
    except Exception as exc:  # noqa: BLE001
        raise RelayError("forced relay could not set the ICE transport policy") from exc

    async def relay_gather(_bound_gatherer: Any) -> None:
        # RTCIceGatherer.gather is an async no-argument method in aiortc 1.15.0.
        await original_gather()
        _assert_relay_only_gathering(connection)

    try:
        gatherer.gather = MethodType(relay_gather, gatherer)
        setattr(gatherer, _PREPARE_MARKER, {"connection": connection})
    except Exception as exc:  # noqa: BLE001
        raise RelayError("forced relay could not wrap the ICE gatherer") from exc


async def verify_forced_relay(
    peer_connection: RTCPeerConnection,
    *,
    timeout: float = 10.0,
) -> tuple[tuple[str, str], ...]:
    """Wait for and validate relay-local nominated ICE pairs.

    Returned diagnostics deliberately contain candidate types only, in
    ``(local_type, remote_type)`` form.  They never expose addresses or TURN
    credentials.
    """
    _require_pinned_versions()
    _gatherer, connection = _locate_private_connection(peer_connection)
    _require_private_fields(
        connection,
        "_transport_policy",
        "_protocols",
        "_local_candidates",
        "_nominated",
    )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout, 0.0)
    while True:
        nominated = connection._nominated
        if not isinstance(nominated, dict):
            raise RelayError("forced relay nominated-pair state is invalid")
        if nominated:
            diagnostics: list[tuple[str, str]] = []
            for pair in nominated.values():
                local_candidate = getattr(pair, "local_candidate", None)
                remote_candidate = getattr(pair, "remote_candidate", None)
                local_type = getattr(local_candidate, "type", None)
                remote_type = getattr(remote_candidate, "type", None)
                if not isinstance(local_type, str) or not isinstance(remote_type, str):
                    raise RelayError("forced relay nominated-pair candidates are invalid")
                if local_type != "relay":
                    raise RelayError("forced relay nominated a non-relay local candidate")
                diagnostics.append((local_type, remote_type))
            return tuple(diagnostics)

        if loop.time() >= deadline:
            raise RelayError("forced relay did not nominate an ICE candidate pair before timeout")
        await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))


def _assert_relay_only_gathering(connection: Any) -> None:
    _require_private_fields(connection, "_protocols", "_local_candidates")
    try:
        protocols = connection._protocols
        candidates = connection._local_candidates
    except TypeError as exc:
        raise RelayError("forced relay ICE candidate collections are invalid") from exc
    if not protocols or not candidates:
        raise RelayError("forced relay gathering produced no relay-only ICE candidates")
    try:
        invalid_selection = any(
            getattr(getattr(protocol, "local_candidate", None), "type", None) != "relay"
            for protocol in protocols
        ) or any(getattr(candidate, "type", None) != "relay" for candidate in candidates)
    except TypeError as exc:
        raise RelayError("forced relay ICE candidate collections are invalid") from exc
    if invalid_selection:
        raise RelayError("forced relay gathering produced non-relay ICE candidates")


def _locate_private_connection(peer_connection: RTCPeerConnection) -> tuple[Any, Any]:
    sctp = getattr(peer_connection, "sctp", None)
    dtls_transport = getattr(sctp, "transport", None)
    ice_transport = getattr(dtls_transport, "transport", None)
    gatherer = getattr(ice_transport, "iceGatherer", None)
    connection = getattr(gatherer, "_connection", None)
    if gatherer is None or connection is None:
        raise RelayError("forced relay requires aiortc SCTP ICE gatherer internals")
    return gatherer, connection


def _require_private_fields(connection: Any, *fields: str) -> None:
    if any(not hasattr(connection, field) for field in fields):
        raise RelayError("forced relay requires aioice connection internals")


def _require_pinned_versions() -> None:
    aiortc_version = getattr(aiortc, "__version__", None)
    aioice_version = getattr(aioice, "__version__", None)
    if aiortc_version != AIORTC_VERSION or aioice_version != AIOICE_VERSION:
        raise RelayError(
            "forced relay requires aiortc==1.15.0 and aioice==0.10.2+uutun1 "
            f"(found aiortc={aiortc_version!r}, aioice={aioice_version!r})"
        )

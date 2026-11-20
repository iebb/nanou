"""Signal connection and same-route, generation-replacing aiortc negotiation."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable
from uuid import UUID

from aiortc import RTCConfiguration, RTCDataChannel, RTCPeerConnection, RTCSessionDescription

from ..api import RoomConfig
from ..signal import SignalCallbacks, SignalConnectionError, SocketIoSignalClient
from .channel import TunnelChannel, TunnelChannelError
from .control import build_control_payload
from .ice_bootstrap import (
    EXTENSION_KEY,
    IceAnswer,
    IceAnswerStatus,
    IceBootstrap,
    IceBootstrapError,
    IceProfilePlan,
    build_ice_profile_plan,
    build_ice_answer_extension,
    encode_ice_bootstrap,
    parse_ice_answer,
    parse_ice_bootstrap,
    to_aiortc_ice_servers,
    validate_ice_answer_echo,
)
from .relay import RelayError, prepare_forced_relay, verify_forced_relay
from .soac import (
    MAX_CANDIDATE_BYTES,
    SoacBuilder,
    SoacMessage,
    SoacRoute,
    build_signal_headers,
    decode_sdp_from_soac,
    parse_soac_data,
)

logger = logging.getLogger("uu-tunnel.session")

CONTROL_ACK_TIMEOUT_S = 10.0
CONTROLLED_OFFER_TIMEOUT_S = 120.0
RELAY_NOMINATION_TIMEOUT_S = 10.0
TUNNEL_CHANNEL_LABEL = "TUNNEL_DATA_CHANNEL"
TUNNEL_CTL_CHANNEL_LABEL = "TUNNEL_CTL_CHANNEL"
MAX_QUEUED_REMOTE_CANDIDATES = 64
MAX_DYNAMIC_CHANNELS = 64
_FORWARD_CHANNEL_LABEL = re.compile(r"FWD/[0-9a-f]{8}\Z")

_MAX_ROUTE_ID_BYTES = 512
_MAX_SDP_MID_BYTES = 256
_SOAC_EVENT_TYPES = frozenset(
    ("offer", "answer", "candidate", "restart_ice", "switch_network_notify")
)
_CANDIDATE_TYPES = frozenset(("host", "srflx", "relay", "prflx"))
_HOST_CANDIDATE_DROP_NETWORKS = (ipaddress.ip_network("169.254.0.0/16"),)
_VIRTUAL_NIC_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"),)
_SANITIZED_RESTART_REASONS = frozenset(
    (
        "active transport failed",
        "remote requested ICE restart",
        "remote requested network switch",
        "remote requested a new ICE generation",
        "ICE restart requires a fresh session",
    )
)


class _AuthorizationState(Enum):
    """Controlled-side local authorization lifecycle."""

    UNAUTHORIZED_IDLE = auto()
    DECISION_IN_FLIGHT = auto()
    AUTHORIZED = auto()


class TunnelSessionError(RuntimeError):
    """Tunnel signal or WebRTC negotiation failed."""


class TunnelControlOutcomeUnknown(TunnelSessionError):
    """A controller ``control`` request may have reached the signal service.

    The message is deliberately static: transport failures can contain signal
    endpoints or request details, neither of which is safe to surface from a
    public session error.
    """

    def __init__(self) -> None:
        super().__init__("controller control outcome is unknown")


class TunnelSoacOutcomeUnknown(TunnelSessionError):
    """A SOAC offer or answer may have reached the peer without an ACK.

    This is distinct from an ordinary negotiation failure because replaying a
    route-bound offer/answer sequence could create a second live attempt.
    """

    def __init__(self) -> None:
        super().__init__("controller SOAC outcome is unknown")


class TunnelNegotiationRetryableError(TunnelSessionError):
    """A fresh room/session attempt may safely retry this negotiation."""


class TunnelProtocolError(TunnelSessionError):
    """A bound control or SOAC protocol invariant was violated."""


class TunnelIceProfilesExhausted(TunnelProtocolError):
    """All safe same-route controller ICE profiles were consumed.

    The count is intentionally the only exposed retry detail: it contains no
    endpoint, username, credential, or per-profile result material.
    """

    def __init__(self, profile_count: int) -> None:
        self.count = profile_count
        self.profile_count = profile_count
        super().__init__(f"ICE endpoint profiles exhausted (count={profile_count})")


class TunnelRestartRequired(TunnelNegotiationRetryableError):
    """An active transport failure or peer restart needs a fresh session."""

    def __init__(self, reason: str = "ICE restart requires a fresh session") -> None:
        # This exception is public, so do not allow an accidental server-provided
        # reason to become observable through its string representation.
        self.reason = (
            reason
            if reason in _SANITIZED_RESTART_REASONS
            else "ICE restart requires a fresh session"
        )
        super().__init__(self.reason)


@dataclass
class ControlResult:
    """Validated fields retained from a controller ``control`` acknowledgement."""

    client_id: str | None = None
    ice_id: str | None = None
    app_control_id: str | None = field(default=None, repr=False)
    ice_servers: list[dict[str, Any]] = field(default_factory=list, repr=False)
    force_relay: bool = False
    code: int | None = None
    msg: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class _QueuedCandidate:
    """A route-bound compatibility candidate retained only for this attempt."""

    route: SoacRoute
    generation: int
    config_id: str
    candidate: dict[str, Any] | None = field(repr=False)
    key: tuple[Any, ...] = field(repr=False)


@dataclass
class _PendingOffer:
    """A complete, validated offer held until its fresh PC can be created."""

    route: SoacRoute
    bootstrap: IceBootstrap
    sdp: str = field(repr=False)
    candidates: list[_QueuedCandidate] = field(default_factory=list, repr=False)
    candidate_keys: set[tuple[Any, ...]] = field(default_factory=set, repr=False)


class _ReplacementOfferAvailable(Exception):
    """Internal wake-up: a bound next controlled offer replaced this attempt."""


class TunnelSession:
    """One room's signal and DataChannel lifecycle.

    A controller emits one control request, pins its ACK route, and may replace
    only the PeerConnection generation across the resulting ordered ICE plan.
    A controlled peer follows those exact sequential replacement offers; it
    never chooses an endpoint profile of its own.
    """

    def __init__(
        self,
        room_config: RoomConfig,
        role: str = "controller",
        assist_request_cb: Callable[[str, str], None] | None = None,
        assist_cancel_cb: Callable[[str], None] | None = None,
        assist_password_cb: Callable[[str, str], None] | None = None,
        assist_control_mode_cb: Callable[[str], None] | None = None,
        force_relay: bool = False,
        filter_virtual_nics: bool = False,
        control_device_id: str | None = None,
        assistance: bool | None = None,
        authorized: bool = True,
    ):
        if role not in ("controller", "controlled"):
            raise ValueError(f"role 必须是 controller/controlled: {role}")
        if type(force_relay) is not bool:
            raise TypeError("force_relay must be a bool")
        if type(filter_virtual_nics) is not bool:
            raise TypeError("filter_virtual_nics must be a bool")

        if role == "controller":
            if not isinstance(control_device_id, str):
                raise TypeError("controller requires control_device_id")
            if not control_device_id or control_device_id != control_device_id.strip():
                raise ValueError("controller control_device_id must be nonempty")
            if type(assistance) is not bool:
                raise TypeError("controller requires assistance as a bool")
        elif type(authorized) is not bool:
            raise TypeError("controlled authorized must be a bool")

        self._room_config = room_config
        self._role = role
        self._assist_request_cb = assist_request_cb
        self._assist_cancel_cb = assist_cancel_cb
        self._assist_password_cb = assist_password_cb
        self._assist_control_mode_cb = assist_control_mode_cb
        self._control_device_id = control_device_id if role == "controller" else None
        self._assistance = assistance if role == "controller" else None
        # A controlled peer obtains policy exclusively from the accepted offer.
        self._force_relay = force_relay if role == "controller" else False
        self._filter_virtual_nics = filter_virtual_nics
        self._effective_policy: str | None = None
        self._profile_plan: IceProfilePlan | None = None
        self._control_sent = False

        self._authorization_state = (
            _AuthorizationState.AUTHORIZED
            if role != "controlled" or authorized
            else _AuthorizationState.UNAUTHORIZED_IDLE
        )
        self._authorization_event = asyncio.Event()
        if self._authorization_state is _AuthorizationState.AUTHORIZED:
            self._authorization_event.set()

        self._signal: SocketIoSignalClient | None = None
        self._signal_available = False
        self._pc: RTCPeerConnection | None = None
        self._channel: RTCDataChannel | None = None
        self._channel_pc: RTCPeerConnection | None = None
        self._channel_wrapper: TunnelChannel | None = None
        self._channel_ready = asyncio.Event()
        self._ctl_channel: RTCDataChannel | None = None
        self._ctl_channel_pc: RTCPeerConnection | None = None
        self._ctl_channel_wrapper: TunnelChannel | None = None
        self._ctl_ready = asyncio.Event()
        self._dynamic_channel_handlers: dict[str, Callable[[TunnelChannel], Any]] = {}
        self._dynamic_channels: set[TunnelChannel] = set()
        self._next_dynamic_stream = 2
        self._dynamic_channel_tasks: set[asyncio.Task[None]] = set()

        self._remote_sdp_event = asyncio.Event()
        self._remote_sdp: str | None = None
        self._remote_offer_route: SoacRoute | None = None
        self._remote_sdp_route: SoacRoute | None = None
        self._expected_route: SoacRoute | None = None
        self._bootstrap: IceBootstrap | None = None
        self._generation: int | None = None
        self._config_id: str | None = None
        self._local_app_control_id: str | None = None
        self._control_ack_pending = False
        self._controller_offer_emitted = False
        self._controller_answer_accepted = False
        self._controller_answer_echo_validated = False
        self._remote_answer: IceAnswer | None = None
        self._remote_answer_event = asyncio.Event()
        self._restart_event = asyncio.Event()
        self._restart_generation: int | None = None
        self._restart_route: SoacRoute | None = None
        self._restart_config_id: str | None = None
        self._attempt_failure: TunnelNegotiationRetryableError | None = None
        self._attempt_failure_event = asyncio.Event()
        self._replacement_offer: _PendingOffer | None = None
        self._replacement_offer_event = asyncio.Event()
        self._quarantined_offer: _PendingOffer | None = None

        self._queued_candidates: list[_QueuedCandidate] = []
        self._candidate_keys: set[tuple[Any, ...]] = set()
        self._remote_candidate_tasks: set[asyncio.Task[None]] = set()
        self._candidate_task_epochs: dict[asyncio.Task[None], int] = {}
        self._candidate_drain_task: asyncio.Task[None] | None = None

        # Every listener captures this generation as well as its object.  A
        # peer-attempt teardown invalidates it before detaching/closing
        # anything.  Signal callbacks use their separate connection epoch so
        # replacing a PC never silences the still-bound signal route.
        self._epoch = 0
        self._signal_epoch = 0
        self._closed_event = asyncio.Event()
        self._closed = False
        self._active = False
        self._resources_closed = False
        self._termination_error: TunnelSessionError | None = None
        self._negotiating = False
        self._negotiated = False
        self._stop_lock = asyncio.Lock()

        self.control_result: ControlResult | None = None

    def begin_authorization_decision(self) -> None:
        """Open one bounded guest-approval decision window.

        The window intentionally does not authorize the peer.  It only allows
        one complete, generation-zero offer (and its exact candidates) to be
        held while the local proof or approval API call reaches a known result.
        """

        if self._role != "controlled":
            raise RuntimeError("only a controlled session can begin authorization")
        self._raise_if_closed()
        if self._authorization_state is not _AuthorizationState.UNAUTHORIZED_IDLE:
            raise RuntimeError("authorization decision is not idle")
        self._clear_authorization_decision_data()
        self._authorization_event.clear()
        self._authorization_state = _AuthorizationState.DECISION_IN_FLIGHT
        logger.info("controlled authorization decision started")

    def mark_authorized(self) -> None:
        """Authorize a controlled peer after a known local decision success."""

        if self._role != "controlled":
            raise RuntimeError("only a controlled session can be authorized")
        self._raise_if_closed()
        if self._authorization_state is _AuthorizationState.AUTHORIZED:
            return
        if self._authorization_state is not _AuthorizationState.DECISION_IN_FLIGHT:
            raise RuntimeError("authorization decision is not in flight")

        # There is no await point between state transition, promotion, and
        # event publication.  An unrelated signal callback therefore cannot
        # replace the selected quarantined offer in this hand-off.
        self._authorization_state = _AuthorizationState.AUTHORIZED
        quarantined = self._quarantined_offer
        self._quarantined_offer = None
        if quarantined is not None:
            self._activate_controlled_offer(quarantined)
        self._authorization_event.set()
        logger.info("controlled authorization gate opened")

    def cancel_authorization_decision(self) -> None:
        """Discard an unresolved guest-approval window without authorizing it."""

        if self._role != "controlled":
            raise RuntimeError("only a controlled session can cancel authorization")
        if self._authorization_state is _AuthorizationState.AUTHORIZED:
            # Cancellation is never a deauthorization operation.
            return
        if self._authorization_state is _AuthorizationState.DECISION_IN_FLIGHT:
            self._authorization_state = _AuthorizationState.UNAUTHORIZED_IDLE
            logger.info("controlled authorization decision canceled")
        # Idle cancellation is deliberately idempotent.  Clearing again also
        # removes any stale data left by a previously interrupted owner.
        self._authorization_event.clear()
        self._clear_authorization_decision_data()

    async def wait_closed(self) -> TunnelSessionError:
        """Wait until termination and return its concrete session error."""

        await self._closed_event.wait()
        return self._termination_exception()

    @property
    def control_channel(self) -> TunnelChannel | None:
        """返回已打开的控制通道；未就绪时为 ``None``。"""

        return self._ctl_channel_wrapper if self._ctl_channel_is_open() else None

    async def wait_control_channel(self, timeout: float | None = None) -> TunnelChannel:
        """等待可选控制通道打开，不影响数据通道协商。"""

        if self._ctl_channel_is_open():
            return self._require_open_control_channel()
        waits: dict[asyncio.Task[bool], str] = {
            asyncio.create_task(self._ctl_ready.wait()): "control",
            asyncio.create_task(self._closed_event.wait()): "closed",
        }
        try:
            done, _pending = await asyncio.wait(
                waits,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError("等待控制 DataChannel 打开 超时")
            labels = {waits[waiter] for waiter in done}
            if "closed" in labels or self._closed:
                raise self._termination_exception()
        finally:
            for waiter in waits:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
        return self._require_open_control_channel()

    async def create_channel(self, label: str) -> TunnelChannel:
        """在活动 PeerConnection 上创建动态 DataChannel；两端都可发起（端口映射不分角色）。"""

        if (
            not isinstance(label, str)
            or not label
            or len(label.encode("utf-8")) > 128
            or label in {TUNNEL_CHANNEL_LABEL, TUNNEL_CTL_CHANNEL_LABEL}
            or (label.startswith("FWD/") and not _FORWARD_CHANNEL_LABEL.fullmatch(label))
        ):
            raise ValueError("dynamic DataChannel label is invalid")
        self._raise_if_closed()
        if len(self._dynamic_channels) >= MAX_DYNAMIC_CHANNELS:
            raise TunnelSessionError("dynamic DataChannel limit reached")
        pc = self._pc
        if pc is None or pc.sctp is None or not self._active:
            raise TunnelSessionError("PeerConnection is not active")

        # Rotate stream IDs. aiortc can free the local ID before the remote
        # stream reset arrives, and an immediate reuse can drop the new OPEN.
        parity = 0 if pc.sctp.is_server else 1
        for _ in range(MAX_DYNAMIC_CHANNELS + 1):
            stream_id = self._next_dynamic_stream * 2 + parity
            if self._next_dynamic_stream == 32766:
                self._next_dynamic_stream = 2
            else:
                self._next_dynamic_stream += 1
            try:
                raw = pc.createDataChannel(label, id=stream_id)
            except ValueError:
                # A stream may still be open after the allocator wraps.
                continue
            break
        else:
            raise TunnelSessionError("No SCTP stream is available for the dynamic channel")
        wrapper = self._wrap_dynamic_channel(raw, pc, self._epoch)
        try:
            await wrapper.wait_ready(timeout=10.0)
            return wrapper
        except BaseException:
            wrapper.close()
            raise

    def set_dynamic_channel_handler(
        self,
        prefix: str,
        callback: Callable[[TunnelChannel], Any],
    ) -> None:
        """注册受控端动态通道前缀处理器。"""

        if not isinstance(prefix, str) or not prefix:
            raise ValueError("dynamic channel prefix is invalid")
        if not callable(callback):
            raise TypeError("dynamic channel handler must be callable")
        self._dynamic_channel_handlers[prefix] = callback

    async def connect_signal(self) -> None:
        """Connect one advertised signal host, failing over only initially."""

        self._raise_if_closed()
        if self._signal is not None:
            if self._signal.connected:
                return
            raise TunnelNegotiationRetryableError(
                "signal transport is unavailable; create a fresh session"
            )

        for signal_server in self._room_config.signal_servers:
            self._raise_if_closed()
            signal = self._new_signal_client()
            self._signal = signal
            self._signal_available = False
            headers = build_signal_headers(
                self._room_config.token,
                controlled=(self._role == "controlled"),
            )
            try:
                await signal.connect(signal_server, headers, timeout=10.0)
            except asyncio.CancelledError:
                await self._discard_signal(signal)
                raise
            except (SignalConnectionError, TimeoutError, OSError):
                # SignalConnectionError intentionally contains only sanitized
                # labels, but do not render any transport exception here.
                logger.warning("信令服务器初始连接失败")
                await self._discard_signal(signal)
                self._raise_if_closed()
                continue
            except Exception:  # noqa: BLE001
                await self._discard_signal(signal)
                error = TunnelNegotiationRetryableError("signal connection failed")
                self._terminate(error)
                raise error from None

            self._raise_if_closed()
            self._signal_available = True
            logger.info("信令已连接 role=%s", self._role)
            return

        error = TunnelNegotiationRetryableError("all advertised signal servers are unavailable")
        self._terminate(error)
        raise error

    async def negotiate(self) -> TunnelChannel:
        """Perform one role-specific control/SOAC/aiortc negotiation pass."""

        self._raise_if_closed()
        if self._signal is None or not self._signal.connected:
            raise TunnelNegotiationRetryableError("a live signal connection is required")
        if self._negotiated:
            return self._require_open_channel()
        if self._negotiating:
            raise TunnelSessionError("会话协商正在进行")

        self._negotiating = True
        try:
            channel = await self._negotiate_once()
            self._negotiated = True
            return channel
        except asyncio.CancelledError:
            self._terminate(TunnelSessionError("会话协商已取消"))
            raise
        except TunnelSessionError as exc:
            self._terminate(exc)
            raise self._termination_exception() from None
        except RelayError:
            if self._closed:
                raise self._termination_exception() from None
            error = TunnelNegotiationRetryableError("forced relay setup or verification failed")
            self._terminate(error)
            raise error from None
        except IceBootstrapError:
            if self._closed:
                raise self._termination_exception() from None
            error = TunnelProtocolError("ICE bootstrap validation failed")
            self._terminate(error)
            raise error from None
        except (SignalConnectionError, TimeoutError, OSError):
            if self._closed:
                raise self._termination_exception() from None
            error = TunnelNegotiationRetryableError("negotiation transport timed out or failed")
            self._terminate(error)
            raise error from None
        except Exception:  # noqa: BLE001
            if self._closed:
                raise self._termination_exception() from None
            error = TunnelNegotiationRetryableError("negotiation failed")
            self._terminate(error)
            raise error from None
        finally:
            self._negotiating = False

    async def start(self) -> TunnelChannel:
        """Convenience lifecycle: connect signal if needed, then negotiate."""

        await self.connect_signal()
        return await self.negotiate()

    async def stop(self) -> None:
        """Idempotently close DataChannel, PeerConnection, and signal transport."""

        async with self._stop_lock:
            if self._resources_closed:
                return
            if not self._closed:
                self._terminate(TunnelSessionError("会话已停止"))
            await self._close_resources()
            self._resources_closed = True

    def _new_signal_client(self) -> SocketIoSignalClient:
        signal_epoch = self._signal_epoch
        signal: SocketIoSignalClient | None = None

        def on_event(event: str, args: list[Any]) -> None:
            self._on_signal_event(event, args, source=signal, epoch=signal_epoch)

        def on_close(reason: str) -> None:
            self._on_signal_closed(reason, source=signal, epoch=signal_epoch)

        def on_error(reason: str) -> None:
            self._on_signal_error(reason, source=signal, epoch=signal_epoch)

        signal = SocketIoSignalClient(
            SignalCallbacks(
                on_event=on_event,
                on_close=on_close,
                on_error=on_error,
            )
        )
        return signal

    async def _discard_signal(self, signal: SocketIoSignalClient) -> None:
        if self._signal is signal:
            self._invalidate_signal_epoch()
            self._invalidate_peer_epoch()
            self._signal = None
            self._signal_available = False
        try:
            await signal.close()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("关闭失败的信令连接")

    async def _negotiate_once(self) -> TunnelChannel:
        if self._role == "controller":
            return await self._negotiate_controller()
        return await self._negotiate_controlled()

    async def _negotiate_controller(self) -> TunnelChannel:
        """Run every ACK-selected ICE profile under one control route.

        A control ACK is a one-shot authorization and routing commitment.  The
        only safe retry inside that commitment is a fresh PeerConnection using
        the next immutable endpoint profile; neither control nor the route is
        replayed here.
        """

        result = await self._send_control()
        self.control_result = result
        self._raise_if_closed()
        if result.code is not None and result.code != 0:
            raise TunnelNegotiationRetryableError("control request was rejected")

        policy = "relay" if self._force_relay or result.force_relay else "all"
        self._effective_policy = policy
        try:
            profile_plan = build_ice_profile_plan(
                result.ice_servers,
                policy=policy,
            )
        except IceBootstrapError:
            raise TunnelProtocolError("control ACK ICE profile is invalid") from None
        self._profile_plan = profile_plan

        route = SoacRoute(
            client_id=result.client_id,
            ice_id=result.ice_id,
            app_control_id=result.app_control_id,
        )
        if not self._route_is_well_formed(route):
            raise TunnelProtocolError("control ACK route is invalid")
        self._pin_route(route, policy)

        for profile_index in range(profile_plan.profile_count):
            # Resetting only replaces the per-generation PC/channel state.  It
            # deliberately preserves the signal connection, control ACK route,
            # effective policy, and immutable profile plan.
            await self._close_peer_attempt()
            self._raise_if_closed()
            try:
                bootstrap = profile_plan.build_bootstrap(
                    index=profile_index,
                    generation=profile_index,
                )
            except IceBootstrapError:
                raise TunnelProtocolError("control ACK ICE profile is invalid") from None
            self._set_attempt_bootstrap(bootstrap)
            logger.info(
                "ICE endpoint profile attempt index=%s count=%s policy=%s",
                profile_index,
                profile_plan.profile_count,
                policy,
            )

            try:
                pc, epoch, local_sdp = await self._create_controller_offer_attempt(
                    bootstrap,
                    route,
                )
            except asyncio.CancelledError:
                raise
            except (RelayError, TimeoutError, TunnelNegotiationRetryableError):
                # No offer has been emitted, so this local allocation/gather
                # failure is known-safe to replace with the next profile.
                continue

            offer_payload = SoacBuilder.offer(
                client_id=route.client_id,
                ice_id=route.ice_id,
                app_control_id=route.app_control_id,
                sdp=local_sdp,
                extension=encode_ice_bootstrap(bootstrap),
            )
            # There is no SOAC ACK.  Mark before the await so an overlapping
            # signal close can never make an attempted offer appear replayable.
            self._controller_offer_emitted = True
            try:
                await self._require_signal().emit_no_ack("soac", offer_payload)
            except asyncio.CancelledError:
                raise
            except (SignalConnectionError, ConnectionError, TimeoutError, OSError, RuntimeError):
                error = TunnelSoacOutcomeUnknown()
                self._terminate(error)
                raise error from None
            self._log_milestone(
                "local-description-emitted",
                pc=pc,
                event_type="offer",
                route=route,
                soac_data=offer_payload["data"],
            )

            response = await self._wait_for_controller_response()
            if response in ("nack", "restart", "failed"):
                # A matching fixed-status NACK, a fully bound restart request,
                # or a local ICE/DTLS/channel failure after a known offer all
                # advance only within this plan and never resend control.
                continue

            remote_sdp = self._remote_sdp
            if remote_sdp is None:
                raise TunnelProtocolError("accepted controller answer is missing SDP")
            try:
                await self._set_remote_description(pc, remote_sdp, "answer")
                self._log_milestone(
                    "remote-description-set",
                    pc=pc,
                    event_type="answer",
                    route=self._remote_sdp_route,
                    plain_sdp=remote_sdp,
                )
                await self._apply_queued_candidates(pc, epoch=epoch)
                return await self._finish_negotiation(pc, bootstrap)
            except asyncio.CancelledError:
                raise
            except (RelayError, TimeoutError, TunnelNegotiationRetryableError):
                # This point follows an exact answer echo.  The same control
                # route remains valid, while a fresh PC is mandatory.
                continue

        await self._close_peer_attempt()
        raise TunnelIceProfilesExhausted(profile_plan.profile_count)

    async def _negotiate_controlled(self) -> TunnelChannel:
        """Follow controller-selected replacement offers on one pinned route."""

        # No PeerConnection is constructed until both local authorization and
        # a strict, bound offer are present.  A decision-window quarantined
        # offer is synchronously promoted by ``mark_authorized``.
        await self._wait_for_event(
            self._authorization_event,
            timeout=None,
            phase="等待本地授权",
        )

        while True:
            await self._wait_for_controlled_offer()
            bootstrap = self._bootstrap
            route = self._expected_route
            remote_sdp = self._remote_sdp
            if bootstrap is None or route is None or remote_sdp is None:
                raise TunnelProtocolError("authorized offer state is incomplete")

            pc: RTCPeerConnection | None = None
            epoch: int | None = None
            try:
                pc = self._new_peer_connection(bootstrap)
                epoch = self._attach_peer_connection(pc)
                await self._set_remote_description(pc, remote_sdp, "offer")
                self._log_milestone(
                    "remote-description-set",
                    pc=pc,
                    event_type="offer",
                    route=self._remote_sdp_route,
                    plain_sdp=remote_sdp,
                )
                if bootstrap.policy == "relay":
                    # setRemoteDescription creates the receiving SCTP transport;
                    # relay-only setup still precedes local gathering.
                    prepare_forced_relay(pc)
                await self._apply_queued_candidates(pc, epoch=epoch)
            except asyncio.CancelledError:
                raise
            except TunnelProtocolError:
                # A syntactically complete but semantically invalid remote SDP
                # is a protocol mismatch, not a local allocation NACK.
                raise
            except (RelayError, TimeoutError, TunnelNegotiationRetryableError):
                await self._emit_controlled_nack(
                    bootstrap,
                    route,
                    IceAnswerStatus.ALLOCATION_FAILED,
                )
                await self._close_peer_attempt()
                continue
            except Exception:  # noqa: BLE001
                await self._emit_controlled_nack(
                    bootstrap,
                    route,
                    IceAnswerStatus.ALLOCATION_FAILED,
                )
                await self._close_peer_attempt()
                continue

            try:
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                local_sdp = self._filter_local_sdp(self._local_description_sdp(pc))
                self._require_local_sdp_candidate_types(local_sdp, "answer")
                self._raise_if_attempt_failed()
            except asyncio.CancelledError:
                raise
            except (RelayError, TimeoutError, TunnelNegotiationRetryableError):
                await self._emit_controlled_nack(
                    bootstrap,
                    route,
                    IceAnswerStatus.GATHER_FAILED,
                )
                await self._close_peer_attempt()
                continue
            except Exception:  # noqa: BLE001
                await self._emit_controlled_nack(
                    bootstrap,
                    route,
                    IceAnswerStatus.GATHER_FAILED,
                )
                await self._close_peer_attempt()
                continue

            self._log_milestone(
                "local-description-set",
                pc=pc,
                event_type="answer",
                route=route,
                plain_sdp=local_sdp,
            )
            await self._emit_controlled_answer(bootstrap, route, local_sdp)

            try:
                return await self._finish_negotiation(pc, bootstrap)
            except asyncio.CancelledError:
                raise
            except _ReplacementOfferAvailable:
                await self._close_peer_attempt()
                self._promote_replacement_offer()
                continue
            except (RelayError, TimeoutError, TunnelNegotiationRetryableError):
                # The exact OK answer was already emitted.  If the signal is
                # still live, tell the controller that this generation failed
                # instead of inventing a controlled-side profile choice.
                if self._replacement_offer_event.is_set():
                    await self._close_peer_attempt()
                    self._promote_replacement_offer()
                    continue
                await self._emit_controlled_nack(
                    bootstrap,
                    route,
                    IceAnswerStatus.ICE_FAILED,
                )
                await self._close_peer_attempt()
                continue

    async def _create_controller_offer_attempt(
        self,
        bootstrap: IceBootstrap,
        route: SoacRoute,
    ) -> tuple[RTCPeerConnection, int, str]:
        """Create a complete local offer before any outcome-unknown emit."""

        pc = self._new_peer_connection(bootstrap)
        epoch = self._attach_peer_connection(pc)
        try:
            channel = pc.createDataChannel(TUNNEL_CHANNEL_LABEL)
            ctl = pc.createDataChannel(TUNNEL_CTL_CHANNEL_LABEL)
            self._register_datachannel(channel, source_pc=pc, epoch=epoch)
            self._register_control_channel(ctl, source_pc=pc, epoch=epoch)
            if bootstrap.policy == "relay":
                prepare_forced_relay(pc)
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            local_sdp = self._filter_local_sdp(self._local_description_sdp(pc))
            self._require_local_sdp_candidate_types(local_sdp, "offer")
            self._raise_if_attempt_failed()
        except asyncio.CancelledError:
            raise
        except (RelayError, TunnelNegotiationRetryableError, TimeoutError):
            raise
        except Exception:
            raise TunnelNegotiationRetryableError("local ICE gathering failed") from None
        self._log_milestone(
            "local-description-set",
            pc=pc,
            event_type="offer",
            route=route,
            plain_sdp=local_sdp,
        )
        return pc, epoch, local_sdp

    def _new_peer_connection(self, bootstrap: IceBootstrap) -> RTCPeerConnection:
        try:
            ice_servers = to_aiortc_ice_servers(bootstrap)
            return RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not stringify a configuration failure: some implementations
            # include the TURN URI or credentials in their diagnostics.
            raise TunnelNegotiationRetryableError(
                "PeerConnection construction failed"
            ) from None

    def _attach_peer_connection(self, pc: RTCPeerConnection) -> int:
        self._raise_if_closed()
        epoch = self._epoch
        self._pc = pc
        pc.on(
            "datachannel",
            lambda channel, *_args: self._on_datachannel(channel, pc, epoch=epoch),
        )
        pc.on(
            "connectionstatechange",
            lambda *_args: self._on_peer_connection_state_change(
                pc, "connectionstatechange", epoch=epoch
            ),
        )
        pc.on(
            "signalingstatechange",
            lambda *_args: self._on_peer_connection_state_change(
                pc, "signalingstatechange", epoch=epoch
            ),
        )
        pc.on(
            "iceconnectionstatechange",
            lambda *_args: self._on_peer_connection_state_change(
                pc, "iceconnectionstatechange", epoch=epoch
            ),
        )
        pc.on(
            "icegatheringstatechange",
            lambda *_args: self._on_peer_connection_state_change(
                pc, "icegatheringstatechange", epoch=epoch
            ),
        )
        return epoch

    async def _set_remote_description(
        self,
        pc: RTCPeerConnection,
        sdp: str,
        sdp_type: str,
    ) -> None:
        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp, sdp_type))
        except asyncio.CancelledError:
            raise
        except Exception:
            raise TunnelProtocolError(
                f"remote {sdp_type} SDP was rejected"
            ) from None
        self._raise_if_closed()
        self._raise_if_attempt_failed()

    async def _finish_negotiation(
        self,
        pc: RTCPeerConnection,
        bootstrap: IceBootstrap,
    ) -> TunnelChannel:
        channel = await self._wait_for_open_channel(timeout=60.0)
        self._raise_if_attempt_failed()
        if bootstrap.policy == "relay":
            try:
                diagnostics = await verify_forced_relay(
                    pc,
                    timeout=RELAY_NOMINATION_TIMEOUT_S,
                )
            except RelayError:
                raise TunnelNegotiationRetryableError(
                    "forced relay verification failed"
                ) from None
            # relay.py returns only (local type, remote type) pairs.
            logger.info("强制 relay ICE 已验证 role=%s pairs=%s", self._role, diagnostics)

        self._raise_if_closed()
        if not self._signal_available:
            raise TunnelNegotiationRetryableError(
                "signal closed before the DataChannel became active"
            )
        self._active = True
        logger.info("WebRTC DataChannel active role=%s policy=%s", self._role, bootstrap.policy)
        return channel

    def _pin_route(self, route: SoacRoute, policy: str) -> None:
        """Bind a session exactly once to its control/SOAC route and policy."""

        if not self._route_is_well_formed(route):
            raise TunnelProtocolError("SOAC route is invalid")
        if policy not in ("all", "relay"):
            raise TunnelProtocolError("ICE policy is invalid")
        if self._expected_route is None:
            self._expected_route = route
            self._effective_policy = policy
            return
        if self._expected_route != route or self._effective_policy != policy:
            raise TunnelProtocolError("SOAC route or ICE policy mismatch")

    def _set_attempt_bootstrap(self, bootstrap: IceBootstrap) -> None:
        """Install one already-authorized generation without changing route."""

        if self._expected_route is None or self._effective_policy is None:
            raise TunnelProtocolError("ICE bootstrap has no pinned route")
        if bootstrap.policy != self._effective_policy:
            raise TunnelProtocolError("ICE bootstrap policy mismatch")
        previous = self._bootstrap
        if (
            previous is not None
            and bootstrap.generation > previous.generation
            and bootstrap.config_id == previous.config_id
        ):
            raise TunnelProtocolError("ICE bootstrap config ID was reused")
        if previous is not None and (
            previous.generation != bootstrap.generation
            or previous.config_id != bootstrap.config_id
        ):
            # A delayed compatibility candidate can arrive while the old PC is
            # being closed.  It must not consume the new generation's bounded
            # queue even though the signal route is intentionally unchanged.
            self._queued_candidates.clear()
            self._candidate_keys.clear()
        self._bootstrap = bootstrap
        self._generation = bootstrap.generation
        self._config_id = bootstrap.config_id

    def _local_description_sdp(self, pc: RTCPeerConnection) -> str:
        description = getattr(pc, "localDescription", None)
        sdp = getattr(description, "sdp", None)
        if not isinstance(sdp, str) or not sdp:
            raise TunnelNegotiationRetryableError("local SDP is unavailable")
        return sdp

    def _filter_local_sdp(self, sdp: str) -> str:
        return "".join(
            line
            for line in sdp.splitlines(keepends=True)
            if not (
                line.rstrip("\r\n").startswith("a=candidate:")
                and self._candidate_line_dropped(line.rstrip("\r\n"))
            )
        )

    def _candidate_addr_dropped(self, addr_str: str) -> bool:
        try:
            address = ipaddress.ip_address(addr_str)
        except ValueError:
            return False
        if address.version != 4:
            return False
        if any(address in network for network in _HOST_CANDIDATE_DROP_NETWORKS):
            return True
        return self._filter_virtual_nics and any(
            address in network for network in _VIRTUAL_NIC_NETWORKS
        )

    def _candidate_line_dropped(self, candidate: str) -> bool:
        canonical = candidate
        if canonical.startswith("a="):
            canonical = canonical[2:]
        if canonical.startswith("candidate:"):
            canonical = canonical[len("candidate:") :]
        parts = canonical.split()
        return (
            len(parts) >= 8
            and parts[6] == "typ"
            and parts[7] == "host"
            and self._candidate_addr_dropped(parts[4])
        )

    def _require_local_sdp_candidate_types(self, sdp: str, description_type: str) -> None:
        if not self._sdp_is_complete(sdp):
            raise TunnelNegotiationRetryableError(
                f"complete local {description_type} SDP lacks ICE candidates or end marker"
            )

    def _raise_if_attempt_failed(self) -> None:
        failure = self._attempt_failure
        if failure is not None:
            raise failure

    async def _wait_for_controller_response(self) -> str:
        """Wait for the exact answer/NACK, a bound restart, or local failure."""

        self._raise_if_closed()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 30.0
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                error = TunnelSoacOutcomeUnknown()
                self._terminate(error)
                raise error
            waits: dict[asyncio.Task[bool], str] = {
                asyncio.create_task(self._remote_answer_event.wait()): "answer",
                asyncio.create_task(self._restart_event.wait()): "restart",
                asyncio.create_task(self._attempt_failure_event.wait()): "failed",
                asyncio.create_task(self._closed_event.wait()): "closed",
            }
            try:
                done, _pending = await asyncio.wait(
                    waits,
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    error = TunnelSoacOutcomeUnknown()
                    self._terminate(error)
                    raise error
                labels = {waits[waiter] for waiter in done}
                if "closed" in labels or self._closed:
                    raise self._termination_exception()
                if "answer" in labels:
                    answer = self._remote_answer
                    if answer is None:
                        raise TunnelProtocolError("controller answer was not retained")
                    return "ok" if answer.status is IceAnswerStatus.OK else "nack"
                if "restart" in labels:
                    if self._consume_current_controller_restart():
                        return "restart"
                    # A stale signal callback can wake this wait after its PC
                    # attempt was disposed.  Consume it and keep waiting for a
                    # route- and generation-bound outcome for this offer.
                    continue
                self._raise_if_attempt_failed()
                return "failed"
            finally:
                for waiter in waits:
                    if not waiter.done():
                        waiter.cancel()
                await asyncio.gather(*waits, return_exceptions=True)

    async def _wait_for_controlled_offer(self) -> None:
        """Wait for the currently promoted complete offer of one generation."""

        if self._remote_sdp is not None:
            return
        if self._replacement_offer is not None and self._pc is None:
            self._promote_replacement_offer()
            return
        await self._wait_for_remote_sdp(
            timeout=CONTROLLED_OFFER_TIMEOUT_S,
            phase="等待 offer",
        )

    async def _emit_controlled_answer(
        self,
        bootstrap: IceBootstrap,
        route: SoacRoute,
        sdp: str,
    ) -> None:
        payload = SoacBuilder.answer(
            client_id=route.client_id,
            ice_id=route.ice_id,
            app_control_id=route.app_control_id,
            sdp=sdp,
            extension=build_ice_answer_extension(bootstrap, IceAnswerStatus.OK),
        )
        try:
            await self._require_signal().emit_no_ack("soac", payload)
        except asyncio.CancelledError:
            raise
        except (SignalConnectionError, ConnectionError, TimeoutError, OSError, RuntimeError):
            error = TunnelSoacOutcomeUnknown()
            self._terminate(error)
            raise error from None
        self._log_milestone(
            "local-description-emitted",
            pc=self._pc,
            event_type="answer",
            route=route,
            soac_data=payload["data"],
        )

    async def _emit_controlled_nack(
        self,
        bootstrap: IceBootstrap,
        route: SoacRoute,
        status: IceAnswerStatus,
    ) -> None:
        """Send one exact route-bound failure extension without SDP material."""

        if not status.is_nack:
            raise TunnelProtocolError("controlled NACK status is invalid")
        payload = SoacBuilder.answer(
            client_id=route.client_id,
            ice_id=route.ice_id,
            app_control_id=route.app_control_id,
            sdp=None,
            extension=build_ice_answer_extension(bootstrap, status),
        )
        try:
            await self._require_signal().emit_no_ack("soac", payload)
        except asyncio.CancelledError:
            raise
        except (SignalConnectionError, ConnectionError, TimeoutError, OSError, RuntimeError):
            # An answer/NACK emit has no acknowledgement.  Once attempted, it
            # cannot be safely replayed on a fresh route.
            error = TunnelSoacOutcomeUnknown()
            self._terminate(error)
            raise error from None
        self._log_milestone(
            "local-description-emitted",
            pc=self._pc,
            event_type="answer",
            route=route,
            soac_data=payload["data"],
        )

    def _promote_replacement_offer(self) -> None:
        pending = self._replacement_offer
        if pending is None:
            return
        self._replacement_offer = None
        self._replacement_offer_event.clear()
        self._activate_controlled_offer(pending)

    def _terminate(self, error: TunnelSessionError) -> None:
        if self._termination_error is None or (
            isinstance(error, (TunnelControlOutcomeUnknown, TunnelSoacOutcomeUnknown))
            and isinstance(self._termination_error, TunnelNegotiationRetryableError)
        ):
            # A close callback can race an in-flight control/offer emit and
            # install the ordinary pre-active signal error first.  The later
            # outcome-unknown classification is more conservative and must win
            # so callers never replay that action.
            self._termination_error = error
        if not self._closed:
            self._closed = True
            self._invalidate_signal_epoch()
            self._invalidate_peer_epoch()
            if self._authorization_state is _AuthorizationState.DECISION_IN_FLIGHT:
                self._authorization_state = _AuthorizationState.UNAUTHORIZED_IDLE
                self._authorization_event.clear()
            self._drop_quarantine()
            self._closed_event.set()

    def _termination_exception(self) -> TunnelSessionError:
        return self._termination_error or TunnelSessionError("会话已终止")

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise self._termination_exception()

    def _require_signal(self) -> SocketIoSignalClient:
        self._raise_if_closed()
        signal = self._signal
        if signal is None or not signal.connected:
            raise TunnelNegotiationRetryableError("signal connection is unavailable")
        return signal

    def _invalidate_peer_epoch(self) -> int:
        self._epoch += 1
        return self._epoch

    def _invalidate_signal_epoch(self) -> int:
        self._signal_epoch += 1
        return self._signal_epoch

    def _is_current_signal(
        self,
        source: SocketIoSignalClient | None,
        epoch: int | None,
    ) -> bool:
        return (
            not self._closed
            and source is not None
            and source is self._signal
            and (epoch is None or epoch == self._signal_epoch)
        )

    def _is_current_pc(self, pc: RTCPeerConnection, epoch: int | None) -> bool:
        return (
            not self._closed
            and pc is self._pc
            and (epoch is None or epoch == self._epoch)
        )

    async def _wait_for_event(
        self,
        event: asyncio.Event,
        *,
        timeout: float | None,
        phase: str,
    ) -> None:
        self._raise_if_closed()
        if event.is_set():
            return
        event_wait = asyncio.create_task(event.wait())
        closed_wait = asyncio.create_task(self._closed_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {event_wait, closed_wait},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError(f"{phase} 超时")
            if closed_wait in done or self._closed:
                raise self._termination_exception()
        finally:
            for waiter in (event_wait, closed_wait):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(event_wait, closed_wait, return_exceptions=True)

    async def _wait_for_remote_sdp(self, *, timeout: float | None, phase: str) -> None:
        try:
            await self._wait_for_event(self._remote_sdp_event, timeout=timeout, phase=phase)
        except TimeoutError:
            if (
                self._role == "controller"
                and phase == "等待 answer"
                and self._controller_offer_emitted
                and not self._controller_answer_accepted
            ):
                error = TunnelSoacOutcomeUnknown()
                self._terminate(error)
                raise error from None
            raise
        if self._remote_sdp is None:
            raise TunnelProtocolError("remote SDP was not retained")

    def _channel_is_open(self) -> bool:
        channel = self._channel
        wrapper = self._channel_wrapper
        return (
            channel is not None
            and wrapper is not None
            and wrapper.raw is channel
            and wrapper.ready
        )

    def _ctl_channel_is_open(self) -> bool:
        channel = self._ctl_channel
        wrapper = self._ctl_channel_wrapper
        return (
            channel is not None
            and wrapper is not None
            and wrapper.raw is channel
            and wrapper.ready
        )

    def _require_open_channel(self) -> TunnelChannel:
        self._raise_if_closed()
        if not self._channel_is_open():
            raise TunnelSessionError("DataChannel 未处于 open 状态")
        return self._channel_wrapper  # type: ignore[return-value]

    def _require_open_control_channel(self) -> TunnelChannel:
        self._raise_if_closed()
        if not self._ctl_channel_is_open():
            raise TunnelSessionError("控制 DataChannel 未处于 open 状态")
        return self._ctl_channel_wrapper  # type: ignore[return-value]

    async def _wait_for_open_channel(self, *, timeout: float | None) -> TunnelChannel:
        if self._channel_is_open():
            return self._require_open_channel()
        waits: dict[asyncio.Task[bool], str] = {
            asyncio.create_task(self._channel_ready.wait()): "channel",
            asyncio.create_task(self._attempt_failure_event.wait()): "failed",
            asyncio.create_task(self._closed_event.wait()): "closed",
        }
        if self._role == "controlled":
            waits[asyncio.create_task(self._replacement_offer_event.wait())] = "replacement"
        try:
            done, _pending = await asyncio.wait(
                waits,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError(
                    f"等待 DataChannel 打开 超时; {self._state_snapshot(self._pc)}"
                ) from None
            labels = {waits[waiter] for waiter in done}
            if "closed" in labels or self._closed:
                raise self._termination_exception()
            if "replacement" in labels:
                raise _ReplacementOfferAvailable()
            if "failed" in labels:
                self._raise_if_attempt_failed()
                raise TunnelNegotiationRetryableError("PeerConnection attempt failed")
        finally:
            for waiter in waits:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
        return self._require_open_channel()

    async def _close_peer_attempt(self) -> None:
        """Dispose only the current PC/DataChannel generation.

        This is intentionally narrower than ``stop``: it keeps the live
        signal client, pinned control route, authorization state, effective
        policy, and controller profile plan.  Every listener from the old
        generation is invalidated before any close operation can synchronously
        dispatch a callback.
        """

        old_epoch = self._epoch
        self._invalidate_peer_epoch()
        self._active = False
        self._channel_ready.clear()
        self._ctl_ready.clear()
        self._remote_sdp_event.clear()
        self._remote_answer_event.clear()
        self._restart_event.clear()
        self._remote_sdp = None
        self._remote_sdp_route = None
        self._remote_offer_route = None
        self._remote_answer = None
        self._restart_generation = None
        self._restart_route = None
        self._restart_config_id = None
        self._controller_offer_emitted = False
        self._controller_answer_accepted = False
        self._controller_answer_echo_validated = False
        self._attempt_failure = None
        self._attempt_failure_event.clear()
        self._queued_candidates.clear()
        self._candidate_keys.clear()
        await self._cancel_remote_candidate_tasks(epoch=old_epoch)

        wrapper = self._channel_wrapper
        self._channel_wrapper = None
        if wrapper is not None:
            try:
                wrapper.detach()
            except Exception:  # noqa: BLE001
                logger.debug("分离 DataChannel 包装器时出错")

        channel = self._channel
        self._channel = None
        self._channel_pc = None
        if channel is not None:
            try:
                channel.close()
            except Exception:  # noqa: BLE001
                logger.debug("关闭 DataChannel 时出错")

        ctl_wrapper = self._ctl_channel_wrapper
        self._ctl_channel_wrapper = None
        if ctl_wrapper is not None:
            try:
                ctl_wrapper.detach()
            except Exception:  # noqa: BLE001
                logger.debug("分离控制 DataChannel 包装器时出错")

        ctl_channel = self._ctl_channel
        self._ctl_channel = None
        self._ctl_channel_pc = None
        if ctl_channel is not None:
            try:
                ctl_channel.close()
            except Exception:  # noqa: BLE001
                logger.debug("关闭控制 DataChannel 时出错")

        dynamic_tasks = tuple(self._dynamic_channel_tasks)
        for task in dynamic_tasks:
            if not task.done():
                task.cancel()
        if dynamic_tasks:
            await asyncio.gather(*dynamic_tasks, return_exceptions=True)
        self._dynamic_channel_tasks.clear()
        for wrapper in tuple(self._dynamic_channels):
            try:
                wrapper.close()
            except Exception:
                logger.debug("关闭动态 DataChannel 时出错")
        self._dynamic_channels.clear()

        pc = self._pc
        self._pc = None
        if pc is not None:
            try:
                await pc.close()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug("关闭 PeerConnection 时出错")

    async def _close_resources(self) -> None:
        # Signal callbacks use a separate epoch because normal peer-attempt
        # replacement must keep accepting SOAC on the same route.
        self._invalidate_signal_epoch()
        self._signal_available = False
        self._drop_quarantine()
        await self._close_peer_attempt()
        await self._cancel_remote_candidate_tasks()

        signal = self._signal
        self._signal = None
        if signal is not None:
            try:
                await signal.close()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug("关闭信令连接时出错")

    async def _cancel_remote_candidate_tasks(self, *, epoch: int | None = None) -> None:
        current_task = asyncio.current_task()
        tasks = tuple(
            task
            for task in self._remote_candidate_tasks
            if task is not current_task
            and (epoch is None or self._candidate_task_epochs.get(task) == epoch)
        )
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._remote_candidate_tasks.difference_update(tasks)
        for task in tasks:
            self._candidate_task_epochs.pop(task, None)
        if self._candidate_drain_task in tasks:
            self._candidate_drain_task = None

    def _drop_quarantine(self) -> None:
        self._quarantined_offer = None

    def _clear_authorization_decision_data(self) -> None:
        """Remove data that can exist only outside an authorized attempt."""

        self._drop_quarantine()
        self._queued_candidates.clear()
        self._candidate_keys.clear()

    async def _send_control(self) -> ControlResult:
        if self._control_sent:
            raise TunnelProtocolError("controller control was already sent")
        control_device_id = self._control_device_id
        assistance = self._assistance
        if control_device_id is None or assistance is None:
            raise TunnelSessionError("controller control parameters are unavailable")
        try:
            payload = build_control_payload(control_device_id, assistance)
        except (TypeError, ValueError):
            raise TunnelSessionError("controller control parameters are invalid") from None

        # build_control_payload creates a canonical lower-case UUID4.  Keep it
        # locally even if the live ACK omits app_control_id.
        if not self._is_canonical_control_id(payload.app_control_id):
            raise TunnelSessionError("controller control ID generation failed")
        # This flag is set before the no-idempotency Socket.IO emit.  A timeout
        # or close therefore remains an outcome-unknown terminal condition,
        # never a reason to generate another control ID.
        self._control_sent = True
        self._local_app_control_id = payload.app_control_id
        signal = self._require_signal()
        self._control_ack_pending = True
        try:
            ack = await signal.emit_with_ack(
                "control",
                payload.socket_payload(),
                ack_timeout=CONTROL_ACK_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            raise
        except (SignalConnectionError, ConnectionError, TimeoutError, OSError, RuntimeError):
            # The control payload has a newly generated ID, but Socket.IO does
            # not let us prove whether a timed-out/disconnected emit reached
            # the remote service.  It is therefore unsafe to replay it.
            error = TunnelControlOutcomeUnknown()
            self._terminate(error)
            raise error from None
        finally:
            self._control_ack_pending = False
        if not isinstance(ack, list):
            raise TunnelProtocolError("control ACK must contain a record")
        record = next((entry for entry in ack if isinstance(entry, Mapping)), None)
        if record is None:
            raise TunnelProtocolError("control ACK must contain a record")

        code: int | None = None
        if "code" in record:
            code_value = record["code"]
            if type(code_value) is not int:
                raise TunnelProtocolError("control ACK code is invalid")
            code = code_value

        message: str | None = None
        if "msg" in record:
            message_value = record["msg"]
            if message_value is not None and not isinstance(message_value, str):
                raise TunnelProtocolError("control ACK message is invalid")
            message = message_value

        # A nonzero controller response is a valid rejection, not a malformed
        # successful ACK.  It has no usable SOAC route or ICE configuration,
        # so do not demand those success-only fields before reporting it.
        if code is not None and code != 0:
            return ControlResult(code=code, msg=message)

        client_id = self._require_route_text(record.get("client_id"), "control ACK client ID")
        ice_id = self._require_route_text(record.get("ice_id"), "control ACK ICE ID")
        if "app_control_id" in record and record["app_control_id"] != payload.app_control_id:
            raise TunnelProtocolError("control ACK app control ID mismatch")

        raw_servers = record.get("iceServers")
        if type(raw_servers) is not list:
            raise TunnelProtocolError("control ACK ICE servers are invalid")
        parsed_ice_servers: list[dict[str, Any]] = []
        for server in raw_servers:
            if not isinstance(server, Mapping):
                raise TunnelProtocolError("control ACK ICE server is invalid")
            # Carry only known WebRTC fields forward.  The bootstrap helper
            # validates their values and keeps credentials memory-only.
            parsed_ice_servers.append(
                {
                    key: server[key]
                    for key in ("urls", "username", "credential", "credentialType")
                    if key in server
                }
            )

        force_relay = False
        if "force_relay" in record:
            force_relay_value = record["force_relay"]
            if type(force_relay_value) is not bool:
                raise TunnelProtocolError("control ACK force_relay is invalid")
            force_relay = force_relay_value

        return ControlResult(
            client_id=client_id,
            ice_id=ice_id,
            app_control_id=payload.app_control_id,
            ice_servers=parsed_ice_servers,
            force_relay=force_relay,
            code=code,
            msg=message,
        )

    def _on_signal_event(
        self,
        event: str,
        args: list[Any],
        source: SocketIoSignalClient | None,
        epoch: int | None = None,
    ) -> None:
        if not self._is_current_signal(source, epoch):
            return
        soac_args, bmsg_args = self._normalize_soac_event(event, args)
        if soac_args is not None:
            self._process_soac_args(soac_args)
            return
        if self._malformed_soac_is_fatal(self._declared_soac_type(args)):
            self._terminate(TunnelProtocolError("SOAC payload is invalid"))
            return
        if self._malformed_soac_is_fatal(event):
            self._terminate(TunnelProtocolError("SOAC payload is invalid"))
            return
        if bmsg_args is not None:
            self._on_bmsg_push(bmsg_args)

    def _normalize_soac_event(
        self,
        event: str,
        args: list[Any],
    ) -> tuple[list[Any] | None, list[Any] | None]:
        """Return normalized SOAC args or one decoded non-SOAC bmsg payload."""

        if event == "soac":
            if isinstance(args, list):
                # Preserve the normal SOAC event's argument boundaries exactly.
                return args, None
            self._debug_malformed_signal_event("soac", "arguments")
            return None, None

        if isinstance(event, str) and event in _SOAC_EVENT_TYPES:
            return self._inject_soac_event_type(args, event, event), None

        if event != "bmsg_push":
            return None, None
        if not isinstance(args, list) or len(args) != 1:
            self._debug_malformed_signal_event("bmsg_push", "arguments")
            return None, None

        raw_push = args[0]
        if isinstance(raw_push, dict):
            push = raw_push
        elif isinstance(raw_push, str):
            try:
                push = json.loads(raw_push)
            except (TypeError, ValueError):
                self._debug_malformed_signal_event("bmsg_push", "json")
                return None, None
            if not isinstance(push, dict):
                self._debug_malformed_signal_event("bmsg_push", "json-shape")
                return None, None
        else:
            self._debug_malformed_signal_event("bmsg_push", "payload")
            return None, None

        push_type = push.get("type")
        if push_type == "soac":
            return self._bmsg_soac_args(push), None
        if isinstance(push_type, str) and push_type in _SOAC_EVENT_TYPES:
            payload_args = self._bmsg_soac_args(push)
            if payload_args is None:
                return None, None
            return self._inject_soac_event_type(payload_args, push_type, "bmsg_push"), None

        # Existing authentication/approval bmsg handling receives a decoded
        # dictionary even when Socket.IO delivered the push as JSON text.
        return None, [push]

    def _bmsg_soac_args(self, push: dict[str, Any]) -> list[Any] | None:
        payload = push.get("data")
        if isinstance(payload, list):
            args = list(payload)
        elif isinstance(payload, dict):
            args = [payload]
        else:
            self._debug_malformed_signal_event("bmsg_push", "soac-payload")
            return None

        # A wrapper route wins over this legacy outer route (parse_soac_data
        # intentionally has that same precedence).
        outer_client_id = push.get("client_id")
        if isinstance(outer_client_id, str) and outer_client_id:
            args.insert(0, outer_client_id)
        return args

    def _inject_soac_event_type(
        self,
        args: list[Any],
        event_type: str,
        event_name: str,
    ) -> list[Any] | None:
        if not isinstance(args, list):
            self._debug_malformed_signal_event(event_name, "arguments")
            return None

        normalized = list(args)
        for index, payload in enumerate(args):
            if not isinstance(payload, dict):
                continue
            if "data" in payload:
                nested = payload.get("data")
                if not isinstance(nested, dict):
                    self._debug_malformed_signal_event(event_name, "wrapper")
                    return None
                copied_wrapper = dict(payload)
                copied_data = dict(nested)
                copied_data["type"] = event_type
                copied_wrapper["data"] = copied_data
                normalized[index] = copied_wrapper
                return normalized

            copied_payload = dict(payload)
            copied_payload["type"] = event_type
            normalized[index] = copied_payload
            return normalized

        self._debug_malformed_signal_event(event_name, "soac-payload")
        return None

    def _process_soac_args(self, args: list[Any]) -> None:
        if self._closed:
            return
        authorization_state = self._authorization_state
        if (
            self._role == "controlled"
            and authorization_state is _AuthorizationState.UNAUTHORIZED_IDLE
        ):
            # Before a local approval/proof submission has begun, no inbound
            # SOAC data is retained or even parsed into session state.
            return
        declared_type = self._declared_soac_type(args)
        message = self._parse_normalized_soac_message(args)
        if message is None:
            if (
                self._role == "controlled"
                and authorization_state is _AuthorizationState.DECISION_IN_FLIGHT
            ):
                # An unresolved decision window retains only a fully validated
                # offer and exact-route candidates; malformed traffic is never
                # allowed to turn the room terminal.
                return
            if self._malformed_soac_is_fatal(declared_type):
                self._terminate(TunnelProtocolError("SOAC payload is invalid"))
                return
            self._debug_malformed_signal_event("soac", "payload")
            return

        try:
            soac_type = message.data.get("type")
            if (
                self._role == "controlled"
                and authorization_state is _AuthorizationState.DECISION_IN_FLIGHT
            ):
                self._quarantine_unauthorized_soac(message)
                return
            if soac_type == "offer":
                if self._role != "controlled":
                    logger.debug("忽略 controller 收到的 offer")
                    return
                self._process_offer(message)
            elif soac_type == "answer":
                if self._role != "controller":
                    logger.debug("忽略 controlled 收到的 answer")
                    return
                self._process_answer(message)
            elif soac_type == "candidate":
                self._process_candidate(message)
            elif soac_type == "restart_ice":
                self._process_restart_ice(message)
            elif soac_type == "switch_network_notify":
                self._process_switch_network_notify(message)
        except TunnelSessionError as exc:
            self._terminate(exc)
        except Exception:  # noqa: BLE001
            # Do not render an arbitrary SOAC payload or parser exception.
            self._terminate(TunnelProtocolError("SOAC message processing failed"))

    def _parse_normalized_soac_message(self, args: list[Any]) -> SoacMessage | None:
        message = parse_soac_data(args)
        if message is not None:
            return message

        # soac.py deliberately recognizes only the original SOAC type set for
        # direct (non-wrapper) shapes.  Accept the network-switch notification
        # here solely so it becomes a typed restart instead of being ignored.
        for index, arg in enumerate(args):
            if not isinstance(arg, dict):
                continue
            nested = arg.get("data")
            data = nested if isinstance(nested, dict) else arg
            if data.get("type") != "switch_network_notify":
                continue
            client_id = arg.get("client_id") if isinstance(arg.get("client_id"), str) else None
            if client_id is None and index > 0 and isinstance(args[index - 1], str):
                client_id = args[index - 1]
            return SoacMessage(
                route=SoacRoute(
                    client_id=client_id,
                    ice_id=data.get("ice_id") if isinstance(data.get("ice_id"), str) else None,
                    app_control_id=(
                        data.get("app_control_id")
                        if isinstance(data.get("app_control_id"), str)
                        else None
                    ),
                ),
                data=data,
            )
        return None

    @staticmethod
    def _declared_soac_type(args: list[Any]) -> str | None:
        for arg in args:
            if not isinstance(arg, dict):
                continue
            nested = arg.get("data")
            data = nested if isinstance(nested, dict) else arg
            soac_type = data.get("type")
            if isinstance(soac_type, str):
                return soac_type
        return None

    def _malformed_soac_is_fatal(self, declared_type: str | None) -> bool:
        if (
            self._role == "controlled"
            and self._authorization_state is not _AuthorizationState.AUTHORIZED
        ):
            return False
        if declared_type == "offer":
            return self._role == "controlled"
        if declared_type == "answer":
            return self._role == "controller" and self._bootstrap is not None
        if declared_type == "candidate":
            return self._expected_route is not None
        return False

    def _quarantine_unauthorized_soac(self, message: SoacMessage) -> None:
        """Retain only one fully validated offer during an active decision."""

        if self._authorization_state is not _AuthorizationState.DECISION_IN_FLIGHT:
            return

        soac_type = message.data.get("type")
        if soac_type == "offer":
            try:
                offer = self._make_pending_offer(message)
            except TunnelSessionError:
                # No route/PC state is pinned before authorization.
                return
            if offer.bootstrap.generation != 0:
                return
            existing = self._quarantined_offer
            if existing is None:
                self._quarantined_offer = offer
                return
            if self._offers_equal(existing, offer):
                return
            # A competing requester must not displace the first bounded offer.
            logger.debug("忽略未授权的额外 SOAC offer")
            return

        if soac_type != "candidate":
            # Restart and network notifications cannot establish a route.
            return
        offer = self._quarantined_offer
        if offer is None or message.route != offer.route:
            return
        try:
            candidate = self._candidate_value(message.data)
            generation, config_id, policy = self._candidate_binding(
                message.data,
                fallback=offer.bootstrap,
            )
            if (
                generation != offer.bootstrap.generation
                or config_id != offer.bootstrap.config_id
                or policy != offer.bootstrap.policy
            ):
                return
            self._append_candidate(
                offer.candidates,
                offer.candidate_keys,
                candidate,
                route=offer.route,
                generation=generation,
                config_id=config_id,
            )
        except TunnelProtocolError as exc:
            # Invalid pre-approval candidate traffic is not allowed to make a
            # guest room terminal.  The one bounded exception is overflowing
            # the retained same-route/generation candidate set.
            if str(exc) == "remote ICE candidate limit exceeded":
                raise
            return

    def _make_pending_offer(self, message: SoacMessage) -> _PendingOffer:
        route = message.route
        if not self._route_is_well_formed(route):
            raise TunnelProtocolError("offer route is invalid")
        bootstrap = self._parse_offer_bootstrap(message.data)
        self._validate_selected_offer_bootstrap(bootstrap)
        sdp = self._decode_complete_remote_sdp(message.data, "offer")
        return _PendingOffer(route=route, bootstrap=bootstrap, sdp=sdp)

    @staticmethod
    def _offers_equal(left: _PendingOffer, right: _PendingOffer) -> bool:
        return (
            left.route == right.route
            and left.bootstrap == right.bootstrap
            and left.sdp == right.sdp
        )

    def _validate_selected_offer_bootstrap(self, bootstrap: IceBootstrap) -> None:
        """Controlled offers must contain one selected, not aggregate, profile."""

        try:
            IceProfilePlan(policy=bootstrap.policy, profiles=(bootstrap.servers,))
        except IceBootstrapError:
            raise TunnelProtocolError("offer ICE bootstrap is not one profile") from None

    def _process_offer(self, message: SoacMessage) -> None:
        if self._role == "controlled" and (
            self._authorization_state is not _AuthorizationState.AUTHORIZED
        ):
            if self._authorization_state is _AuthorizationState.DECISION_IN_FLIGHT:
                self._quarantine_unauthorized_soac(message)
            return
        offer = self._make_pending_offer(message)
        expected = self._expected_route
        current = self._bootstrap
        if expected is None:
            if offer.bootstrap.generation != 0:
                raise TunnelProtocolError("first controlled offer generation is invalid")
            self._activate_controlled_offer(offer)
            return

        if offer.route != expected:
            raise TunnelProtocolError("offer route mismatch")
        if current is None:
            raise TunnelProtocolError("controlled route has no current generation")
        if offer.bootstrap.policy != self._effective_policy:
            raise TunnelProtocolError("offer ICE policy mismatch")
        if offer.bootstrap.generation < current.generation:
            logger.debug("忽略较旧 ICE generation 的 offer")
            return
        if offer.bootstrap.generation == current.generation:
            if offer.bootstrap != current:
                raise TunnelProtocolError("offer ICE bootstrap changed within generation")
            known = self._replacement_offer
            if known is not None and known.bootstrap.generation == current.generation:
                if not self._offers_equal(known, offer):
                    raise TunnelProtocolError("offer SDP changed within the pinned generation")
                return
            if self._remote_sdp is not None and self._remote_sdp != offer.sdp:
                raise TunnelProtocolError("offer SDP changed within the pinned generation")
            return
        if offer.bootstrap.generation != current.generation + 1:
            raise TunnelProtocolError("controlled offer generation is not sequential")
        if offer.bootstrap.config_id == current.config_id:
            raise TunnelProtocolError("controlled offer config ID was reused")

        pending = self._replacement_offer
        if pending is not None:
            if self._offers_equal(pending, offer):
                return
            raise TunnelProtocolError("multiple controlled replacement offers were received")
        if self._pc is None and self._remote_sdp is None:
            self._activate_controlled_offer(offer)
            return
        self._replacement_offer = offer
        self._replacement_offer_event.set()

    def _activate_controlled_offer(self, offer: _PendingOffer) -> None:
        """Promote one validated offer without constructing its PeerConnection."""

        expected = self._expected_route
        if expected is None:
            if offer.bootstrap.generation != 0:
                raise TunnelProtocolError("first controlled offer generation is invalid")
            self._pin_route(offer.route, offer.bootstrap.policy)
        elif offer.route != expected:
            raise TunnelProtocolError("offer route mismatch")
        else:
            current = self._bootstrap
            if current is None:
                raise TunnelProtocolError("controlled route has no current generation")
            if offer.bootstrap.policy != self._effective_policy:
                raise TunnelProtocolError("offer ICE policy mismatch")
            if offer.bootstrap.generation != current.generation + 1:
                raise TunnelProtocolError("controlled offer generation is not sequential")
            if offer.bootstrap.config_id == current.config_id:
                raise TunnelProtocolError("controlled offer config ID was reused")
        self._set_attempt_bootstrap(offer.bootstrap)
        self._remote_sdp = offer.sdp
        self._remote_sdp_route = offer.route
        self._remote_offer_route = offer.route
        self._remote_sdp_event.clear()
        self._queued_candidates = list(offer.candidates)
        self._candidate_keys = set(offer.candidate_keys)
        self._remote_sdp_event.set()
        self._log_milestone(
            "remote-description-accepted",
            pc=self._pc,
            event_type="offer",
            route=offer.route,
        )

    def _process_answer(self, message: SoacMessage) -> None:
        bootstrap = self._bootstrap
        expected = self._expected_route
        if bootstrap is None or expected is None or not self._controller_offer_emitted:
            # It predates this controller attempt and cannot be route-bound.
            logger.debug("忽略没有活动 offer 的 answer")
            return
        if message.route != expected:
            raise TunnelProtocolError("answer route mismatch")
        answer = self._parse_answer(message.data)
        if (
            answer.generation != bootstrap.generation
            or answer.config_id != bootstrap.config_id
            or answer.policy != bootstrap.policy
        ):
            if answer.generation < bootstrap.generation:
                logger.debug("忽略较旧 ICE generation 的 answer")
                return
            raise TunnelProtocolError("answer ICE bootstrap echo mismatch")
        # Keep the helper as the single strict echo rule even though the field
        # comparison above is needed to classify stale generations as ignored.
        if validate_ice_answer_echo(bootstrap, answer) is not None:
            raise TunnelProtocolError("answer ICE bootstrap echo mismatch")
        self._controller_answer_echo_validated = True

        previous = self._remote_answer
        if previous is not None and previous != answer:
            raise TunnelProtocolError("answer status changed within generation")
        if answer.status.is_nack:
            if not self._answer_has_no_sdp(message.data):
                raise TunnelProtocolError("ICE answer NACK must not contain SDP")
            self._remote_answer = answer
            self._controller_answer_accepted = True
            self._remote_answer_event.set()
            return

        sdp = self._decode_complete_remote_sdp(message.data, "answer")
        if self._remote_sdp is not None:
            if sdp != self._remote_sdp:
                raise TunnelProtocolError("answer SDP changed within the pinned generation")
            return
        self._remote_answer = answer
        self._remote_sdp = sdp
        self._remote_sdp_route = message.route
        self._controller_answer_accepted = True
        self._remote_answer_event.set()
        self._remote_sdp_event.set()
        self._log_milestone(
            "remote-description-accepted",
            pc=self._pc,
            event_type="answer",
            route=message.route,
        )

    def _process_restart_ice(self, message: SoacMessage) -> None:
        """Accept restart only after a route pin and generation/config proof."""

        expected = self._expected_route
        current = self._bootstrap
        if expected is None or current is None or message.route != expected:
            return
        if self._role == "controlled":
            # A controlled peer never selects a profile; a restart request is
            # usable only when it is a complete next-generation replacement
            # offer carrying the controller-selected bootstrap.
            self._process_offer(message)
            return
        self._queue_controller_profile_replacement(message, event_name="restart")

    def _process_switch_network_notify(self, message: SoacMessage) -> None:
        """Accept a switch only as one bound next-generation replacement."""

        self._queue_controller_profile_replacement(message, event_name="switch")

    def _queue_controller_profile_replacement(
        self,
        message: SoacMessage,
        *,
        event_name: str,
    ) -> None:
        """Queue one disposable, exact-route request for the next profile."""

        expected = self._expected_route
        current = self._bootstrap
        if (
            self._role != "controller"
            or expected is None
            or current is None
            or not self._controller_offer_emitted
        ):
            # Notifications cannot establish a route or revive a disposed
            # controller attempt.
            return
        if not self._route_is_well_formed(message.route) or message.route != expected:
            return
        try:
            replacement = self._parse_restart_identity(message.data)
        except TunnelProtocolError:
            raise TunnelProtocolError(f"{event_name} ICE extension is invalid") from None
        if replacement.generation <= current.generation:
            logger.debug("忽略较旧 ICE generation 的 %s 通知", event_name)
            return
        if (
            replacement.generation != current.generation + 1
            or replacement.config_id == current.config_id
            or replacement.policy != current.policy
        ):
            raise TunnelProtocolError(f"{event_name} ICE generation or config is invalid")
        if self._restart_event.is_set():
            if (
                self._restart_route == message.route
                and self._restart_config_id == current.config_id
                and self._restart_generation == replacement.generation
            ):
                return
            raise TunnelProtocolError("multiple controller restart requests were received")
        self._restart_route = message.route
        self._restart_config_id = current.config_id
        self._restart_generation = replacement.generation
        self._restart_event.set()

    def _consume_current_controller_restart(self) -> bool:
        """Consume a restart wake-up only when it still names this offer."""

        generation = self._restart_generation
        route = self._restart_route
        config_id = self._restart_config_id
        self._restart_generation = None
        self._restart_route = None
        self._restart_config_id = None
        self._restart_event.clear()

        current = self._bootstrap
        valid = (
            not self._closed
            and self._role == "controller"
            and self._controller_offer_emitted
            and current is not None
            and route == self._expected_route
            and config_id == current.config_id
            and generation == current.generation + 1
        )
        if not valid:
            logger.debug("丢弃已失效的 controller ICE replacement 通知")
        return valid

    def _parse_restart_identity(self, data: Mapping[str, Any]) -> IceAnswer | IceBootstrap:
        """Parse the route-bound next-generation identity without diagnostics."""

        extension = data.get(EXTENSION_KEY)
        if not isinstance(extension, Mapping):
            raise TunnelProtocolError("restart ICE extension is invalid")
        try:
            answer = parse_ice_answer(extension)
        except IceBootstrapError:
            try:
                return parse_ice_bootstrap(extension)
            except IceBootstrapError:
                raise TunnelProtocolError("restart ICE extension is invalid") from None
        if answer.status is not IceAnswerStatus.RESTART_REQUIRED:
            raise TunnelProtocolError("restart ICE status is invalid")
        return answer

    def _process_candidate(self, message: SoacMessage) -> None:
        expected = self._expected_route
        if expected is None or self._bootstrap is None or message.route != expected:
            # Compatibility candidates cannot establish a route.  In
            # particular, a delayed pre-pin/old-route candidate must not turn
            # a fresh generation into a protocol failure.
            return
        candidate = self._candidate_value(message.data)
        binding = self._candidate_binding(message.data)
        pending = self._replacement_offer
        if pending is not None and message.route == pending.route:
            generation, config_id, policy = self._candidate_binding(
                message.data,
                fallback=pending.bootstrap,
            )
            if (
                generation == pending.bootstrap.generation
                and config_id == pending.bootstrap.config_id
                and policy == pending.bootstrap.policy
            ):
                self._append_candidate(
                    pending.candidates,
                    pending.candidate_keys,
                    candidate,
                    route=pending.route,
                    generation=generation,
                    config_id=config_id,
                )
                return
        self._handle_remote_candidate(
            candidate,
            route=message.route,
            binding=binding,
        )

    @staticmethod
    def _candidate_value(data: Mapping[str, Any]) -> Any:
        candidate: Any = data.get("candidate")
        if isinstance(candidate, str):
            return {
                key: data[key]
                for key in ("candidate", "sdpMid", "sdpMLineIndex")
                if key in data
            }
        return candidate

    def _candidate_binding(
        self,
        data: Mapping[str, Any],
        *,
        fallback: IceBootstrap | None = None,
    ) -> tuple[int, str, str]:
        """Read optional compatibility binding, or bind to one current offer."""

        parsed: IceAnswer | IceBootstrap | None = None
        extension = data.get(EXTENSION_KEY)
        if extension is not None:
            if not isinstance(extension, Mapping):
                raise TunnelProtocolError("candidate ICE binding is invalid")
            try:
                parsed = parse_ice_answer(extension)
            except IceBootstrapError:
                try:
                    parsed = parse_ice_bootstrap(extension)
                except IceBootstrapError:
                    raise TunnelProtocolError("candidate ICE binding is invalid") from None

        root_values = tuple(data.get(name) for name in ("generation", "config_id", "policy"))
        root_present = any(name in data for name in ("generation", "config_id", "policy"))
        if root_present:
            if not all(name in data for name in ("generation", "config_id", "policy")):
                raise TunnelProtocolError("candidate ICE binding is invalid")
            try:
                root = IceAnswer(
                    generation=root_values[0],
                    config_id=root_values[1],
                    policy=root_values[2],
                )
            except IceBootstrapError:
                raise TunnelProtocolError("candidate ICE binding is invalid") from None
            if parsed is not None and (
                root.generation != parsed.generation
                or root.config_id != parsed.config_id
                or root.policy != parsed.policy
            ):
                raise TunnelProtocolError("candidate ICE binding mismatch")
            parsed = root

        if parsed is None:
            parsed = fallback or self._bootstrap
        if parsed is None:
            raise TunnelProtocolError("candidate has no bound ICE generation")
        return parsed.generation, parsed.config_id, parsed.policy

    def _parse_offer_bootstrap(self, data: Mapping[str, Any]) -> IceBootstrap:
        extension = data.get(EXTENSION_KEY)
        if not isinstance(extension, Mapping):
            raise TunnelProtocolError("offer is missing its ICE bootstrap")
        try:
            return parse_ice_bootstrap(extension)
        except IceBootstrapError:
            raise TunnelProtocolError("offer ICE bootstrap is invalid") from None

    def _parse_answer(self, data: Mapping[str, Any]) -> IceAnswer:
        extension = data.get(EXTENSION_KEY)
        if not isinstance(extension, Mapping):
            raise TunnelProtocolError("answer is missing its ICE bootstrap echo")
        try:
            return parse_ice_answer(extension)
        except IceBootstrapError:
            raise TunnelProtocolError("answer ICE bootstrap echo is invalid") from None

    @staticmethod
    def _answer_has_no_sdp(data: Mapping[str, Any]) -> bool:
        return "gzip_sdp" not in data and data.get("sdp", "") in ("", None)

    def _decode_complete_remote_sdp(self, data: Mapping[str, Any], description_type: str) -> str:
        sdp = decode_sdp_from_soac(dict(data))
        if sdp is None:
            # decode_sdp_from_soac performs the compressed/decompressed bounds;
            # do not reduce this to a later negotiation timeout.
            raise TunnelProtocolError(
                f"remote {description_type} SDP is missing or exceeds protocol bounds"
            )
        if not self._sdp_is_complete(sdp):
            raise TunnelProtocolError(
                f"complete remote {description_type} SDP lacks ICE candidates or end marker"
            )
        return sdp

    @staticmethod
    def _sdp_candidate_types(sdp: str) -> frozenset[str]:
        types: set[str] = set()
        for line in sdp.splitlines():
            if not line.startswith("a=candidate:"):
                continue
            parts = line[len("a=candidate:") :].split()
            for index, value in enumerate(parts[:-1]):
                if value == "typ" and parts[index + 1] in _CANDIDATE_TYPES:
                    types.add(parts[index + 1])
                    break
        return frozenset(types)

    @classmethod
    def _sdp_is_complete(cls, sdp: str) -> bool:
        return bool(cls._sdp_candidate_types(sdp)) and any(
            line.strip() == "a=end-of-candidates" for line in sdp.splitlines()
        )

    def _append_candidate(
        self,
        queue: list[_QueuedCandidate],
        keys: set[tuple[Any, ...]],
        candidate: Any,
        *,
        route: SoacRoute,
        generation: int,
        config_id: str,
    ) -> None:
        normalised, key = self._normalise_remote_candidate(candidate)
        if normalised is not None and self._remote_candidate_dropped(normalised):
            return
        route_key = (
            route.client_id,
            route.ice_id,
            route.app_control_id,
            generation,
            config_id,
            *key,
        )
        if route_key in keys:
            return
        if len(keys) >= MAX_QUEUED_REMOTE_CANDIDATES:
            raise TunnelProtocolError("remote ICE candidate limit exceeded")
        keys.add(route_key)
        queue.append(
            _QueuedCandidate(
                route=route,
                generation=generation,
                config_id=config_id,
                candidate=normalised,
                key=route_key,
            )
        )

    def _handle_remote_candidate(
        self,
        candidate: Any,
        route: SoacRoute | None = None,
        binding: tuple[int, str, str] | None = None,
    ) -> None:
        """Queue one bounded compatibility candidate for one exact generation."""

        if self._closed:
            return
        expected = self._expected_route
        bootstrap = self._bootstrap
        if expected is None or bootstrap is None:
            logger.debug("丢弃尚未绑定 route 的 ICE candidate")
            return
        route = expected if route is None else route
        if route != expected:
            # A delayed candidate from a stale route is not a protocol failure
            # for the fresh same-session generation.
            logger.debug("忽略 route 不匹配的旧 ICE candidate")
            return
        generation, config_id, policy = binding or (
            bootstrap.generation,
            bootstrap.config_id,
            bootstrap.policy,
        )
        if (
            generation != bootstrap.generation
            or config_id != bootstrap.config_id
            or policy != bootstrap.policy
        ):
            logger.debug("忽略 generation 不匹配的旧 ICE candidate")
            return
        self._append_candidate(
            self._queued_candidates,
            self._candidate_keys,
            candidate,
            route=route,
            generation=generation,
            config_id=config_id,
        )

        pc = self._pc
        if pc is not None and getattr(pc, "remoteDescription", None) is not None:
            self._start_remote_candidate_task(pc, self._epoch)

    @staticmethod
    def _normalise_remote_candidate(
        value: Any,
    ) -> tuple[dict[str, Any] | None, tuple[Any, ...]]:
        source: Mapping[str, Any] | None
        if value is None:
            return None, ("end-of-candidates",)
        if isinstance(value, str):
            source = None
            raw = value
        elif isinstance(value, Mapping):
            source = value
            raw = value.get("candidate")
            if raw is None:
                return None, ("end-of-candidates",)
        else:
            raise TunnelProtocolError("remote ICE candidate is invalid")

        if not isinstance(raw, str):
            raise TunnelProtocolError("remote ICE candidate is invalid")
        try:
            if len(raw.encode("utf-8")) > MAX_CANDIDATE_BYTES:
                raise TunnelProtocolError("remote ICE candidate exceeds the limit")
        except UnicodeEncodeError:
            raise TunnelProtocolError("remote ICE candidate is invalid") from None

        canonical = raw.strip()
        if canonical.startswith("a="):
            canonical = canonical[2:]
        if canonical.startswith("candidate:"):
            canonical = canonical[len("candidate:") :]
        canonical = " ".join(canonical.split())
        if not canonical:
            return None, ("end-of-candidates",)

        sdp_mid: str | None = None
        sdp_mline_index: int | None = None
        if source is not None:
            raw_mid = source.get("sdpMid")
            if raw_mid is not None:
                if not isinstance(raw_mid, str):
                    raise TunnelProtocolError("remote ICE candidate is invalid")
                try:
                    if len(raw_mid.encode("utf-8")) > _MAX_SDP_MID_BYTES:
                        raise TunnelProtocolError("remote ICE candidate is invalid")
                except UnicodeEncodeError:
                    raise TunnelProtocolError("remote ICE candidate is invalid") from None
                sdp_mid = raw_mid

            raw_mline = source.get("sdpMLineIndex")
            if raw_mline is not None:
                if type(raw_mline) is int:
                    sdp_mline_index = raw_mline
                elif (
                    isinstance(raw_mline, str)
                    and raw_mline.isascii()
                    and raw_mline.isdigit()
                ):
                    sdp_mline_index = int(raw_mline)
                else:
                    raise TunnelProtocolError("remote ICE candidate is invalid")
                if not 0 <= sdp_mline_index <= 65535:
                    raise TunnelProtocolError("remote ICE candidate is invalid")

        normalised: dict[str, Any] = {"candidate": canonical}
        if sdp_mid is not None:
            normalised["sdpMid"] = sdp_mid
        if sdp_mline_index is not None:
            normalised["sdpMLineIndex"] = sdp_mline_index
        return normalised, ("candidate", canonical, sdp_mid, sdp_mline_index)

    def _remote_candidate_dropped(self, candidate: dict[str, Any]) -> bool:
        value = candidate.get("candidate")
        return isinstance(value, str) and self._candidate_line_dropped(value)

    def _start_remote_candidate_task(self, pc: RTCPeerConnection, epoch: int) -> None:
        if not self._queued_candidates or not self._is_current_pc(pc, epoch):
            return
        existing = self._candidate_drain_task
        if existing is not None and not existing.done():
            return
        self._candidate_drain_task = None
        task = asyncio.create_task(self._add_remote_candidate_later(pc, epoch))
        self._remote_candidate_tasks.add(task)
        self._candidate_task_epochs[task] = epoch
        self._candidate_drain_task = task

        def discard_completed(completed: asyncio.Task[None]) -> None:
            self._remote_candidate_tasks.discard(completed)
            self._candidate_task_epochs.pop(completed, None)
            if self._candidate_drain_task is completed:
                self._candidate_drain_task = None

        task.add_done_callback(discard_completed)

    async def _apply_queued_candidates(
        self,
        pc: RTCPeerConnection,
        *,
        epoch: int | None = None,
    ) -> None:
        callback_epoch = self._epoch if epoch is None else epoch
        while self._queued_candidates:
            if not self._is_current_pc(pc, callback_epoch):
                return
            queued, self._queued_candidates = self._queued_candidates, []
            for entry in queued:
                if (
                    entry.route != self._expected_route
                    or entry.generation != self._generation
                    or entry.config_id != self._config_id
                ):
                    # A queued compatibility candidate is tied to its arrival
                    # generation.  Reset/replacement never lets it reach a new
                    # PeerConnection even if the signal route itself is same.
                    continue
                await self._add_remote_candidate(entry.candidate, pc, epoch=callback_epoch)
                self._raise_if_closed()
                self._raise_if_attempt_failed()

    async def _add_remote_candidate_later(
        self,
        pc: RTCPeerConnection,
        epoch: int,
    ) -> None:
        try:
            await self._apply_queued_candidates(pc, epoch=epoch)
        except asyncio.CancelledError:
            raise
        except TunnelSessionError as exc:
            if self._is_current_pc(pc, epoch):
                self._terminate(exc)
        except Exception:  # noqa: BLE001
            if self._is_current_pc(pc, epoch):
                self._terminate(TunnelProtocolError("remote ICE candidate application failed"))

    async def _add_remote_candidate(
        self,
        candidate: dict[str, Any] | None,
        pc: RTCPeerConnection,
        *,
        epoch: int | None = None,
    ) -> None:
        if not self._is_current_pc(pc, epoch):
            return
        try:
            if candidate is None:
                await pc.addIceCandidate(None)
                return
            from aiortc.sdp import candidate_from_sdp

            parsed = candidate_from_sdp(candidate["candidate"])
            if isinstance(candidate.get("sdpMid"), str):
                parsed.sdpMid = candidate["sdpMid"]
            if type(candidate.get("sdpMLineIndex")) is int:
                parsed.sdpMLineIndex = candidate["sdpMLineIndex"]
            await pc.addIceCandidate(parsed)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise TunnelProtocolError("remote ICE candidate is invalid") from None

    def _debug_malformed_signal_event(self, event: str, reason: str) -> None:
        logger.debug(
            "忽略格式无效的信令事件 role=%s event=%s reason=%s",
            self._role,
            event,
            reason,
        )

    def _on_bmsg_push(self, args: list[Any]) -> None:
        if len(args) != 1 or not isinstance(args[0], dict):
            return
        message = args[0]
        push_type = message.get("type")
        if push_type == "get_control_mode":
            data = message.get("data")
            if not isinstance(data, dict):
                return
            control_id = data.get("control_id")
            if (
                not isinstance(control_id, str)
                or not control_id
                or self._assist_control_mode_cb is None
            ):
                return
            try:
                self._assist_control_mode_cb(control_id)
            except Exception:  # noqa: BLE001
                logger.exception("control mode 查询回调异常")
            return
        if push_type == "remote_control":
            data = message.get("data")
            if not isinstance(data, dict):
                return
            control_id = data.get("control_id")
            salt = data.get("salt")
            if (
                not isinstance(control_id, str)
                or not control_id
                or not isinstance(salt, str)
                or not salt
                or self._assist_password_cb is None
            ):
                return
            try:
                self._assist_password_cb(control_id, salt)
            except Exception:  # noqa: BLE001
                logger.exception("协助密码回调异常")
            return
        if push_type not in (
            "remote_control_by_confirmation",
            "remote_control_by_confirmation_cancel",
        ):
            return
        data = message.get("data")
        if not isinstance(data, dict):
            return
        control_id = data.get("control_id")
        if not isinstance(control_id, str) or not control_id:
            return

        if push_type == "remote_control_by_confirmation":
            if self._assist_request_cb is None:
                return
            name = data.get("name")
            if not isinstance(name, str):
                name = "?"
            try:
                self._assist_request_cb(control_id, name)
            except Exception:  # noqa: BLE001
                logger.exception("协助请求回调异常")
        elif self._assist_cancel_cb is not None:
            try:
                self._assist_cancel_cb(control_id)
            except Exception:  # noqa: BLE001
                logger.exception("协助取消回调异常")

    def _on_signal_error(
        self,
        _reason: str,
        source: SocketIoSignalClient | None,
        epoch: int | None = None,
    ) -> None:
        if self._is_current_signal(source, epoch):
            logger.warning("信令报告错误 role=%s", self._role)

    def _on_signal_closed(
        self,
        _reason: str,
        source: SocketIoSignalClient | None,
        epoch: int | None = None,
    ) -> None:
        if not self._is_current_signal(source, epoch):
            return
        self._signal_available = False
        if self._active:
            # The tunnel is now PC/DataChannel-owned.  A signal outage cannot
            # retroactively invalidate its established transport.
            logger.warning("活动 DataChannel 后信令断开；继续保持隧道")
            return
        logger.warning("DataChannel 激活前信令断开")
        if self._role == "controller" and self._control_ack_pending:
            self._terminate(TunnelControlOutcomeUnknown())
            return
        if (
            self._role == "controller"
            and self._controller_offer_emitted
            and not self._controller_answer_accepted
        ):
            self._terminate(TunnelSoacOutcomeUnknown())
            return
        self._terminate(
            TunnelNegotiationRetryableError(
                "signal closed before the DataChannel became active"
            )
        )

    def _on_peer_connection_state_change(
        self,
        pc: RTCPeerConnection,
        event_type: str = "statechange",
        *,
        epoch: int | None = None,
    ) -> None:
        if not self._is_current_pc(pc, epoch):
            return
        self._log_pc_state(pc, event_type)
        if getattr(pc, "iceConnectionState", None) == "failed":
            self._record_attempt_failure(
                TunnelNegotiationRetryableError("ICE connection failed")
            )
        elif getattr(pc, "connectionState", None) == "failed":
            self._record_attempt_failure(
                TunnelNegotiationRetryableError("PeerConnection failed")
            )
        elif (
            getattr(pc, "connectionState", None) == "closed"
            or getattr(pc, "signalingState", None) == "closed"
        ):
            self._record_attempt_failure(
                TunnelNegotiationRetryableError("PeerConnection closed")
            )

    def _record_attempt_failure(self, error: TunnelNegotiationRetryableError) -> None:
        """Wake the role loop for a fresh PC, unless its tunnel is active."""

        if self._active:
            self._terminate(TunnelRestartRequired("active transport failed"))
            return
        if self._attempt_failure is None:
            self._attempt_failure = error
            self._attempt_failure_event.set()

    def _on_datachannel(
        self,
        channel: RTCDataChannel,
        source_pc: RTCPeerConnection,
        *,
        epoch: int | None = None,
    ) -> None:
        """aiortc emits the RTCDataChannel directly for incoming channels."""

        if not self._is_current_pc(source_pc, epoch):
            return
        label = getattr(channel, "label", None)
        if label == TUNNEL_CHANNEL_LABEL:
            if self._channel is not None and self._channel is not channel:
                logger.warning("关闭重复的 tunnel DataChannel")
                _close_quietly(channel)
                return
            if self._channel is None:
                self._log_milestone(
                    "datachannel-received",
                    pc=source_pc,
                    event_type="datachannel",
                    route=None,
                )
                self._register_datachannel(channel, source_pc=source_pc, epoch=epoch)
            return
        if label == TUNNEL_CTL_CHANNEL_LABEL:
            if self._ctl_channel is not None and self._ctl_channel is not channel:
                logger.warning("关闭重复的控制 DataChannel")
                _close_quietly(channel)
                return
            if self._ctl_channel is None:
                self._log_milestone(
                    "control-datachannel-received",
                    pc=source_pc,
                    event_type="datachannel",
                    route=None,
                )
                self._register_control_channel(channel, source_pc=source_pc, epoch=epoch)
            return
        if isinstance(label, str):
            handler = next(
                (
                    callback
                    for prefix, callback in self._dynamic_channel_handlers.items()
                    if label.startswith(prefix)
                ),
                None,
            )
            if handler is not None and (
                not label.startswith("FWD/")
                or _FORWARD_CHANNEL_LABEL.fullmatch(label)
            ):
                if (
                    len(self._dynamic_channels) >= MAX_DYNAMIC_CHANNELS
                    or len(self._dynamic_channel_tasks) >= MAX_DYNAMIC_CHANNELS
                ):
                    logger.warning("关闭超限动态 DataChannel label=%r", label)
                    channel.close()
                    return
                wrapper = self._wrap_dynamic_channel(
                    channel,
                    source_pc,
                    self._epoch if epoch is None else epoch,
                )
                task = asyncio.create_task(
                    self._run_dynamic_channel_handler(handler, wrapper),
                    name=f"uu-tunnel-dynamic-{label}",
                )
                self._dynamic_channel_tasks.add(task)
                task.add_done_callback(self._dynamic_channel_tasks.discard)
                task.add_done_callback(_consume_background_task)
                return
        logger.warning("关闭未知 DataChannel label=%r", label)
        _close_quietly(channel)

    def _wrap_dynamic_channel(
        self,
        channel: RTCDataChannel,
        source_pc: RTCPeerConnection,
        epoch: int,
    ) -> TunnelChannel:
        wrapper: TunnelChannel

        def remove() -> None:
            self._dynamic_channels.discard(wrapper)

        wrapper = TunnelChannel(
            channel,
            lambda error: logger.debug("动态 DataChannel 失败: %s", error),
            on_close=remove,
            binary_min=0,
            binary_max=65_536,
            activate=False,
        )
        if not self._is_current_pc(source_pc, epoch):
            wrapper.close()
            raise TunnelSessionError("dynamic DataChannel generation is stale")
        self._dynamic_channels.add(wrapper)
        wrapper.activate()
        return wrapper

    async def _run_dynamic_channel_handler(
        self,
        handler: Callable[[TunnelChannel], Any],
        channel: TunnelChannel,
    ) -> None:
        try:
            result = handler(channel)
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("动态 DataChannel 处理失败", exc_info=True)
        finally:
            channel.close()

    def _register_datachannel(
        self,
        channel: RTCDataChannel,
        source_pc: RTCPeerConnection | None = None,
        *,
        epoch: int | None = None,
    ) -> None:
        pc = self._pc if source_pc is None else source_pc
        callback_epoch = self._epoch if epoch is None else epoch
        if pc is None or not self._is_current_pc(pc, callback_epoch):
            return
        if self._channel is not None and self._channel is not channel:
            return
        self._channel = channel
        self._channel_pc = pc
        wrapper = TunnelChannel(
            channel,
            lambda error: self._on_channel_failure(
                channel,
                error,
                source_pc=pc,
                epoch=callback_epoch,
            ),
            on_open=lambda: self._on_channel_open(
                channel,
                source_pc=pc,
                epoch=callback_epoch,
            ),
            on_close=lambda: self._on_channel_closed(
                channel,
                source_pc=pc,
                epoch=callback_epoch,
            ),
            activate=False,
        )
        self._channel_wrapper = wrapper
        # TunnelChannel bound its listeners synchronously before activation.
        wrapper.activate()

    def _register_control_channel(
        self,
        channel: RTCDataChannel,
        source_pc: RTCPeerConnection | None = None,
        *,
        epoch: int | None = None,
    ) -> None:
        pc = self._pc if source_pc is None else source_pc
        callback_epoch = self._epoch if epoch is None else epoch
        if pc is None or not self._is_current_pc(pc, callback_epoch):
            return
        if self._ctl_channel is not None and self._ctl_channel is not channel:
            return
        self._ctl_channel = channel
        self._ctl_channel_pc = pc
        wrapper = TunnelChannel(
            channel,
            lambda error: self._on_ctl_channel_failure(
                channel,
                error,
                source_pc=pc,
                epoch=callback_epoch,
            ),
            on_open=lambda: self._on_ctl_channel_open(
                channel,
                source_pc=pc,
                epoch=callback_epoch,
            ),
            on_close=lambda: self._on_ctl_channel_closed(
                channel,
                source_pc=pc,
                epoch=callback_epoch,
            ),
            activate=False,
        )
        self._ctl_channel_wrapper = wrapper
        wrapper.activate()

    def _on_channel_open(
        self,
        channel: RTCDataChannel,
        *,
        source_pc: RTCPeerConnection | None = None,
        epoch: int | None = None,
    ) -> None:
        pc = self._pc if source_pc is None else source_pc
        wrapper = self._channel_wrapper
        if (
            pc is not None
            and self._is_current_pc(pc, epoch)
            and channel is self._channel
            and self._channel_pc is pc
            and wrapper is not None
            and wrapper.raw is channel
            and wrapper.ready
        ):
            self._log_milestone(
                "datachannel-open",
                pc=pc,
                event_type="datachannel",
                route=None,
            )
            self._channel_ready.set()

    def _on_channel_closed(
        self,
        channel: RTCDataChannel,
        *,
        source_pc: RTCPeerConnection | None = None,
        epoch: int | None = None,
    ) -> None:
        pc = self._pc if source_pc is None else source_pc
        if (
            pc is not None
            and self._is_current_pc(pc, epoch)
            and channel is self._channel
            and self._channel_pc is pc
        ):
            self._record_attempt_failure(
                TunnelNegotiationRetryableError("DataChannel closed")
            )

    def _on_channel_failure(
        self,
        channel: RTCDataChannel,
        _error: TunnelChannelError,
        *,
        source_pc: RTCPeerConnection | None = None,
        epoch: int | None = None,
    ) -> None:
        pc = self._pc if source_pc is None else source_pc
        if (
            pc is not None
            and self._is_current_pc(pc, epoch)
            and channel is self._channel
            and self._channel_pc is pc
        ):
            self._record_attempt_failure(
                TunnelNegotiationRetryableError("DataChannel failed")
            )

    def _on_ctl_channel_open(
        self,
        channel: RTCDataChannel,
        *,
        source_pc: RTCPeerConnection | None = None,
        epoch: int | None = None,
    ) -> None:
        pc = self._pc if source_pc is None else source_pc
        wrapper = self._ctl_channel_wrapper
        if (
            pc is not None
            and self._is_current_pc(pc, epoch)
            and channel is self._ctl_channel
            and self._ctl_channel_pc is pc
            and wrapper is not None
            and wrapper.raw is channel
            and wrapper.ready
        ):
            self._log_milestone(
                "control-datachannel-open",
                pc=pc,
                event_type="datachannel",
                route=None,
            )
            self._ctl_ready.set()

    def _on_ctl_channel_closed(
        self,
        channel: RTCDataChannel,
        *,
        source_pc: RTCPeerConnection | None = None,
        epoch: int | None = None,
    ) -> None:
        pc = self._pc if source_pc is None else source_pc
        if (
            pc is not None
            and self._is_current_pc(pc, epoch)
            and channel is self._ctl_channel
            and self._ctl_channel_pc is pc
        ):
            self._ctl_ready.clear()
            logger.warning("控制 DataChannel 已关闭；游客桥接不可用")

    def _on_ctl_channel_failure(
        self,
        channel: RTCDataChannel,
        _error: TunnelChannelError,
        *,
        source_pc: RTCPeerConnection | None = None,
        epoch: int | None = None,
    ) -> None:
        pc = self._pc if source_pc is None else source_pc
        if (
            pc is not None
            and self._is_current_pc(pc, epoch)
            and channel is self._ctl_channel
            and self._ctl_channel_pc is pc
        ):
            self._ctl_ready.clear()
            logger.warning("控制 DataChannel 不可用；游客桥接不可用")

    @staticmethod
    def _require_route_text(value: Any, field_name: str) -> str:
        if not isinstance(value, str) or not value or value != value.strip():
            raise TunnelProtocolError(f"{field_name} is invalid")
        try:
            if len(value.encode("utf-8")) > _MAX_ROUTE_ID_BYTES:
                raise TunnelProtocolError(f"{field_name} is invalid")
        except UnicodeEncodeError:
            raise TunnelProtocolError(f"{field_name} is invalid") from None
        return value

    @classmethod
    def _route_is_well_formed(cls, route: SoacRoute) -> bool:
        try:
            cls._require_route_text(route.client_id, "route client ID")
            cls._require_route_text(route.ice_id, "route ICE ID")
        except TunnelProtocolError:
            return False
        return cls._is_canonical_control_id(route.app_control_id)

    @staticmethod
    def _is_canonical_control_id(value: Any) -> bool:
        if type(value) is not str:
            return False
        try:
            parsed = UUID(value)
        except (AttributeError, TypeError, ValueError):
            return False
        return parsed.version == 4 and str(parsed) == value

    def _log_milestone(
        self,
        milestone: str,
        *,
        pc: RTCPeerConnection | None,
        event_type: str,
        route: SoacRoute | None,
        plain_sdp: str | None = None,
        soac_data: dict[str, Any] | None = None,
    ) -> None:
        plain_length, compressed_length = self._sdp_lengths(plain_sdp, soac_data)
        route = route or SoacRoute()
        logger.info(
            "WebRTC milestone role=%s event=%s milestone=%s "
            "route_client=%s route_ice=%s route_app=%s "
            "sdp_plain_len=%s sdp_gzip_len=%s %s",
            self._role,
            event_type,
            milestone,
            route.client_id is not None,
            route.ice_id is not None,
            route.app_control_id is not None,
            plain_length if plain_length is not None else "none",
            compressed_length if compressed_length is not None else "none",
            self._candidate_type_snapshot(pc),
        )

    def _log_pc_state(self, pc: RTCPeerConnection, event_type: str) -> None:
        logger.info(
            "WebRTC state role=%s event=%s %s",
            self._role,
            event_type,
            self._state_snapshot(pc),
        )

    def _state_snapshot(self, pc: RTCPeerConnection | None) -> str:
        if pc is None:
            return (
                "connection=none ice=none gathering=none signaling=none "
                "dtls=none sctp=none local_candidates=none remote_candidates=none"
            )
        sctp = getattr(pc, "sctp", None)
        dtls_transport = getattr(sctp, "transport", None)
        return (
            "connection=%s ice=%s gathering=%s signaling=%s dtls=%s sctp=%s %s"
            % (
                self._safe_state(getattr(pc, "connectionState", None)),
                self._safe_state(getattr(pc, "iceConnectionState", None)),
                self._safe_state(getattr(pc, "iceGatheringState", None)),
                self._safe_state(getattr(pc, "signalingState", None)),
                self._safe_state(getattr(dtls_transport, "state", None)),
                self._safe_state(getattr(sctp, "state", None)),
                self._candidate_type_snapshot(pc),
            )
        )

    def _candidate_type_snapshot(self, pc: RTCPeerConnection | None) -> str:
        if pc is None:
            return "local_candidates=none remote_candidates=none"
        sctp = getattr(pc, "sctp", None)
        dtls_transport = getattr(sctp, "transport", None)
        ice_transport = getattr(dtls_transport, "transport", None)
        gatherer = getattr(ice_transport, "iceGatherer", None)

        local_candidates: Any = None
        remote_candidates: Any = None
        try:
            get_local = getattr(gatherer, "getLocalCandidates", None)
            if callable(get_local):
                local_candidates = get_local()
            get_remote = getattr(ice_transport, "getRemoteCandidates", None)
            if callable(get_remote):
                remote_candidates = get_remote()
        except Exception:  # noqa: BLE001
            return "local_candidates=unknown remote_candidates=unknown"
        return (
            f"local_candidates={self._candidate_type_counts(local_candidates)} "
            f"remote_candidates={self._candidate_type_counts(remote_candidates)}"
        )

    @staticmethod
    def _candidate_type_counts(candidates: Any) -> str:
        if candidates is None:
            return "none"
        counts: dict[str, int] = {}
        try:
            for candidate in candidates:
                candidate_type = getattr(candidate, "type", None)
                if candidate_type not in _CANDIDATE_TYPES:
                    candidate_type = "other"
                counts[candidate_type] = counts.get(candidate_type, 0) + 1
        except TypeError:
            return "unknown"
        if not counts:
            return "none"
        return ",".join(f"{kind}:{counts[kind]}" for kind in sorted(counts))

    @staticmethod
    def _safe_state(value: Any) -> str:
        return value if isinstance(value, str) and value else "none"

    @staticmethod
    def _sdp_lengths(
        plain_sdp: str | None,
        soac_data: dict[str, Any] | None,
    ) -> tuple[int | None, int | None]:
        plain_length = len(plain_sdp) if isinstance(plain_sdp, str) else None
        compressed_length: int | None = None
        if isinstance(soac_data, dict):
            data_sdp = soac_data.get("sdp")
            if plain_length is None and isinstance(data_sdp, str):
                plain_length = len(data_sdp)
            gzip_sdp = soac_data.get("gzip_sdp")
            if isinstance(gzip_sdp, (bytes, bytearray)):
                compressed_length = len(gzip_sdp)
        return plain_length, compressed_length


def _close_quietly(channel: RTCDataChannel) -> None:
    """关闭多余或未知的 DataChannel，忽略 aiortc 关闭期异常。"""

    try:
        channel.close()
    except Exception:
        logger.debug("关闭 DataChannel 时出错", exc_info=True)


def _consume_background_task(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        return


__all__ = [
    "TunnelSession",
    "TunnelSessionError",
    "TunnelControlOutcomeUnknown",
    "TunnelSoacOutcomeUnknown",
    "TunnelNegotiationRetryableError",
    "TunnelProtocolError",
    "TunnelIceProfilesExhausted",
    "TunnelRestartRequired",
]

"""Strict, secret-safe ICE bootstrap extension helpers.

The extension deliberately has a small, canonical schema.  It is not a
general WebRTC configuration parser: callers turn the control ACK's known ICE
server fields into :class:`IceBootstrap`, put its encoded form under
``x_uu_tunnel`` on an offer, and require a matching :class:`IceAnswer` on the
answer.  The models and all validation failures avoid rendering credentials.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from aiortc import RTCIceServer


# Kept independently useful so SOAC can carry the extension without knowing
# any ICE-server details.
EXTENSION_KEY = "x_uu_tunnel"
ICE_BOOTSTRAP_VERSION = 1

MAX_ICE_SERVERS = 8
MAX_URLS_PER_ICE_SERVER = 8
MAX_ICE_TEXT_BYTES = 512
MAX_ICE_GENERATION = 16
MAX_ICE_PROFILES = 4

_BOOTSTRAP_KEYS = frozenset(
    ("version", "kind", "generation", "config_id", "policy", "ice_servers")
)
_ANSWER_KEYS = frozenset(("version", "kind", "generation", "config_id", "policy", "status"))
_SERVER_KEYS = frozenset(("urls", "username", "credential", "credentialType"))
_POLICIES = frozenset(("all", "relay"))
_MISSING = object()


class IceBootstrapError(ValueError):
    """A sanitized ICE bootstrap validation error.

    Error messages intentionally describe only the invalid field category;
    they never interpolate an ICE URL, username, credential, or parser error.
    """


class IceBootstrapStatus(str, Enum):
    """Sanitized result for non-throwing parsing and answer-echo checks."""

    INVALID = "invalid"
    MISMATCH = "mismatch"


class IceAnswerStatus(str, Enum):
    """The fixed, credential-free outcome of one ICE bootstrap offer.

    ``ok`` carries a normal SDP answer.  Every other value is an explicit
    negative acknowledgement and therefore deliberately carries no diagnostic
    text, ICE URL, username, or credential.  Keeping this as a wire enum (and
    not an open-ended reason string) lets a controller distinguish a safe
    same-route profile replacement from a malformed answer.
    """

    OK = "ok"
    GATHER_FAILED = "gather-failed"
    ALLOCATION_FAILED = "allocation-failed"
    ICE_FAILED = "ice-failed"
    RESTART_REQUIRED = "restart-required"

    @property
    def is_nack(self) -> bool:
        """Whether this status is a retryable, route-bound NACK."""

        return self is not IceAnswerStatus.OK


@dataclass(frozen=True)
class IceServerConfig:
    """One validated ICE server configuration.

    ``username`` and ``credential`` are excluded from dataclass rendering.
    URLs cannot contain userinfo and bootstrap rendering excludes the whole
    server tuple, so normal ``repr``/``str`` cannot disclose TURN secrets.
    """

    urls: tuple[str, ...]
    username: str | None = field(default=None, repr=False)
    credential: str | None = field(default=None, repr=False)
    credential_type: str = "password"

    def __post_init__(self) -> None:
        _validate_server_values(
            self.urls,
            self.username,
            self.credential,
            self.credential_type,
        )


@dataclass(frozen=True)
class IceBootstrap:
    """Validated controller ICE configuration carried by an offer."""

    generation: int
    config_id: str
    policy: str
    servers: tuple[IceServerConfig, ...] = field(repr=False)

    def __post_init__(self) -> None:
        _validate_generation(self.generation)
        _validate_config_id(self.config_id)
        _validate_policy(self.policy)
        if type(self.servers) is not tuple:
            _invalid("invalid ICE server collection")
        if not self.servers or len(self.servers) > MAX_ICE_SERVERS:
            _invalid("invalid ICE server count")
        if any(not isinstance(server, IceServerConfig) for server in self.servers):
            _invalid("invalid ICE server")
        if self.policy == "relay" and not any(_server_has_turn(server) for server in self.servers):
            _invalid("relay policy requires TURN")


@dataclass(frozen=True)
class IceProfilePlan:
    """Bounded, endpoint-redacted controller ICE attempt profiles.

    Each profile contains at most the first ACK STUN endpoint and one TURN
    endpoint.  The profile contents are intentionally omitted from ``repr`` so
    diagnostics can expose only the safe profile count and policy.
    """

    policy: str
    profiles: tuple[tuple[IceServerConfig, ...], ...] = field(repr=False)

    def __post_init__(self) -> None:
        _validate_policy(self.policy)
        if type(self.profiles) is not tuple:
            _invalid("invalid ICE profile collection")
        if not self.profiles or len(self.profiles) > MAX_ICE_PROFILES:
            _invalid("invalid ICE profile count")

        first_stun: tuple[str, str | None, str | None] | None = None
        turn_endpoints: set[tuple[str, str | None, str | None]] = set()
        previous_turn_rank = -1
        has_turn = False
        has_stun_only = False
        has_turn_with_stun = False
        has_turn_without_stun = False

        for profile in self.profiles:
            stun_server, turn_server = _validate_ice_profile(profile, self.policy)
            if stun_server is not None:
                stun_endpoint = _ice_endpoint_identity(stun_server)
                if first_stun is None:
                    first_stun = stun_endpoint
                elif stun_endpoint != first_stun:
                    _invalid("ICE profile STUN association is invalid")

            if turn_server is None:
                has_stun_only = True
                continue

            has_turn = True
            if stun_server is None:
                has_turn_without_stun = True
            else:
                has_turn_with_stun = True
            turn_endpoint = _ice_endpoint_identity(turn_server)
            if turn_endpoint in turn_endpoints:
                _invalid("duplicate ICE profile")
            turn_endpoints.add(turn_endpoint)

            turn_rank = _turn_url_rank(turn_server.urls[0])
            if turn_rank < previous_turn_rank:
                _invalid("ICE profile transport order is invalid")
            previous_turn_rank = turn_rank

        if self.policy == "relay" and not has_turn:
            _invalid("relay policy requires TURN")
        if has_turn and (has_stun_only or (has_turn_with_stun and has_turn_without_stun)):
            _invalid("ICE profile STUN association is invalid")
        if not has_turn and (self.policy != "all" or not has_stun_only or len(self.profiles) != 1):
            _invalid("invalid ICE profile collection")

    @property
    def profile_count(self) -> int:
        """Return the safe number of available endpoint attempts."""

        return len(self.profiles)

    def select(self, index: int) -> tuple[IceServerConfig, ...]:
        """Return one immutable profile without rendering endpoint details."""

        if type(index) is not int or index < 0:
            _invalid("invalid ICE profile index")
        if index >= len(self.profiles):
            _invalid("ICE profile index is unavailable")
        return self.profiles[index]

    def build_bootstrap(self, index: int, generation: int) -> IceBootstrap:
        """Build a fresh, selected-profile bootstrap for one controller offer."""

        _validate_generation(generation)
        return IceBootstrap(
            generation=generation,
            config_id=str(uuid4()),
            policy=self.policy,
            servers=self.select(index),
        )


@dataclass(frozen=True)
class IceAnswer:
    """Validated, credential-free acknowledgement of an :class:`IceBootstrap`."""

    generation: int
    config_id: str
    policy: str
    status: IceAnswerStatus | str = IceAnswerStatus.OK

    def __post_init__(self) -> None:
        _validate_generation(self.generation)
        _validate_config_id(self.config_id)
        _validate_policy(self.policy)
        try:
            status = (
                self.status
                if isinstance(self.status, IceAnswerStatus)
                else IceAnswerStatus(self.status)
            )
        except (TypeError, ValueError):
            _invalid("invalid ICE answer status")
        # Preserve source compatibility for callers which constructed an
        # ``IceAnswer`` with ``status=\"ok\"`` while exposing a strict enum to
        # all new consumers.
        object.__setattr__(self, "status", status)


def build_ice_profile_plan(
    raw_ice_servers: Sequence[Mapping[str, Any]],
    policy: str = "all",
) -> IceProfilePlan:
    """Build up to four ordered, single-TURN controller attempt profiles.

    Raw ACK records are fully validated before endpoint selection.  Individual
    URL lists are split because pinned aiortc consumes only the first STUN and
    TURN URL.  Within each transport class ACK order is retained, while TURN
    UDP precedes TURN TCP, which precedes TURN-over-TLS/TCP.
    """

    _validate_policy(policy)
    servers = _parse_ack_ice_servers(raw_ice_servers)

    first_stun: IceServerConfig | None = None
    turn_servers: list[tuple[int, IceServerConfig]] = []
    seen_endpoints: set[tuple[str, str | None, str | None]] = set()

    for server in servers:
        for url in server.urls:
            endpoint = IceServerConfig(
                urls=(url,),
                username=server.username,
                credential=server.credential,
                credential_type=server.credential_type,
            )
            endpoint_key = _ice_endpoint_identity(endpoint)
            if endpoint_key in seen_endpoints:
                continue
            seen_endpoints.add(endpoint_key)

            scheme = _ice_url_scheme(url)
            if scheme == "stun":
                if first_stun is None:
                    first_stun = endpoint
                continue
            turn_servers.append((_turn_url_rank(url), endpoint))

    profiles: list[tuple[IceServerConfig, ...]] = []
    for _rank, turn_server in sorted(turn_servers, key=lambda item: item[0]):
        profile = (turn_server,) if first_stun is None else (first_stun, turn_server)
        profiles.append(profile)
        if len(profiles) == MAX_ICE_PROFILES:
            break

    if not profiles:
        if policy == "relay":
            _invalid("relay policy requires TURN")
        if first_stun is None:
            _invalid("invalid ICE profile collection")
        # A STUN-only profile is useful only when no TURN attempt exists; do
        # not add an empty/host-only fallback after cross-NAT TURN profiles.
        profiles.append((first_stun,))

    return IceProfilePlan(policy=policy, profiles=tuple(profiles))

def encode_ice_bootstrap(bootstrap: IceBootstrap) -> dict[str, Any]:
    """Return the exact offer extension dictionary for ``bootstrap``.

    The result has no expiry or implementation-specific fields.  Server
    records are canonical and always contain the four known WebRTC fields.
    """

    if not isinstance(bootstrap, IceBootstrap):
        _invalid("invalid ICE bootstrap")
    return {
        "version": ICE_BOOTSTRAP_VERSION,
        "kind": "ice-bootstrap",
        "generation": bootstrap.generation,
        "config_id": bootstrap.config_id,
        "policy": bootstrap.policy,
        "ice_servers": [_encode_server(server) for server in bootstrap.servers],
    }

def parse_ice_bootstrap(extension: Mapping[str, Any]) -> IceBootstrap:
    """Strictly parse one offer extension without mutating its input."""

    _require_exact_mapping(extension, _BOOTSTRAP_KEYS, "invalid ICE bootstrap")
    if type(extension["version"]) is not int or extension["version"] != ICE_BOOTSTRAP_VERSION:
        _invalid("unsupported ICE bootstrap version")
    if type(extension["kind"]) is not str or extension["kind"] != "ice-bootstrap":
        _invalid("invalid ICE bootstrap kind")
    generation = extension["generation"]
    config_id = extension["config_id"]
    policy = extension["policy"]
    _validate_generation(generation)
    _validate_config_id(config_id)
    _validate_policy(policy)

    raw_servers = extension["ice_servers"]
    if type(raw_servers) is not list:
        _invalid("invalid ICE server collection")
    if not raw_servers or len(raw_servers) > MAX_ICE_SERVERS:
        _invalid("invalid ICE server count")
    servers = tuple(_parse_wire_server(raw_server) for raw_server in raw_servers)
    return IceBootstrap(generation, config_id, policy, servers)

def to_aiortc_ice_servers(
    bootstrap_or_servers: IceBootstrap | tuple[IceServerConfig, ...],
) -> list[RTCIceServer]:
    """Create fresh aiortc ``RTCIceServer`` objects without logging secrets."""

    if isinstance(bootstrap_or_servers, IceBootstrap):
        servers = bootstrap_or_servers.servers
    elif type(bootstrap_or_servers) is tuple and all(
        isinstance(server, IceServerConfig) for server in bootstrap_or_servers
    ):
        servers = bootstrap_or_servers
    else:
        _invalid("invalid ICE server collection")

    # Keep this import local: basic parsing/validation remains usable without
    # constructing an aiortc peer connection.
    from aiortc import RTCIceServer

    return [
        RTCIceServer(
            urls=list(server.urls),
            username=server.username,
            credential=server.credential,
            credentialType=server.credential_type,
        )
        for server in servers
    ]

def build_ice_answer(
    bootstrap: IceBootstrap,
    status: IceAnswerStatus | str = IceAnswerStatus.OK,
) -> IceAnswer:
    """Create a credential-free answer or fixed-status NACK for an offer."""

    if not isinstance(bootstrap, IceBootstrap):
        _invalid("invalid ICE bootstrap")
    return IceAnswer(
        generation=bootstrap.generation,
        config_id=bootstrap.config_id,
        policy=bootstrap.policy,
        status=status,
    )


def encode_ice_answer(answer: IceAnswer) -> dict[str, Any]:
    """Return the exact, credential-free answer extension dictionary."""

    if not isinstance(answer, IceAnswer):
        _invalid("invalid ICE answer")
    return {
        "version": ICE_BOOTSTRAP_VERSION,
        "kind": "ice-answer",
        "generation": answer.generation,
        "config_id": answer.config_id,
        "policy": answer.policy,
        "status": answer.status.value,
    }


def build_ice_answer_extension(
    bootstrap: IceBootstrap,
    status: IceAnswerStatus | str = IceAnswerStatus.OK,
) -> dict[str, Any]:
    """Create and encode a successful answer or fixed-status NACK in one step."""

    return encode_ice_answer(build_ice_answer(bootstrap, status=status))


def parse_ice_answer(extension: Mapping[str, Any]) -> IceAnswer:
    """Strictly parse one credential-free answer extension."""

    _require_exact_mapping(extension, _ANSWER_KEYS, "invalid ICE answer")
    if type(extension["version"]) is not int or extension["version"] != ICE_BOOTSTRAP_VERSION:
        _invalid("unsupported ICE answer version")
    if type(extension["kind"]) is not str or extension["kind"] != "ice-answer":
        _invalid("invalid ICE answer kind")
    return IceAnswer(
        generation=extension["generation"],
        config_id=extension["config_id"],
        policy=extension["policy"],
        status=extension["status"],
    )

def try_parse_ice_answer(
    extension: Mapping[str, Any],
) -> tuple[IceAnswer | None, IceBootstrapStatus | None]:
    """Parse an answer without returning raw parser errors or endpoint data."""

    try:
        return parse_ice_answer(extension), None
    except IceBootstrapError:
        return None, IceBootstrapStatus.INVALID

def validate_ice_answer_echo(
    bootstrap: IceBootstrap,
    answer: IceAnswer | Mapping[str, Any],
) -> IceBootstrapStatus | None:
    """Check that an answer exactly echoes an offer's non-secret identity.

    ``None`` is success.  Failures are deliberately reduced to a sanitized
    enum, so session code never needs to log a raw parse exception or URL.
    """

    if not isinstance(bootstrap, IceBootstrap):
        return IceBootstrapStatus.INVALID
    if isinstance(answer, IceAnswer):
        parsed_answer = answer
    else:
        parsed_answer, status = try_parse_ice_answer(answer)
        if status is not None:
            return status
        assert parsed_answer is not None
    if (
        parsed_answer.generation != bootstrap.generation
        or parsed_answer.config_id != bootstrap.config_id
        or parsed_answer.policy != bootstrap.policy
    ):
        return IceBootstrapStatus.MISMATCH
    return None

def _parse_ack_ice_servers(
    raw_ice_servers: Sequence[Mapping[str, Any]],
) -> tuple[IceServerConfig, ...]:
    """Validate and canonically copy only known ACK ICE server fields."""

    if not isinstance(raw_ice_servers, (list, tuple)):
        _invalid("invalid ICE server collection")
    if not raw_ice_servers or len(raw_ice_servers) > MAX_ICE_SERVERS:
        _invalid("invalid ICE server count")

    servers: list[IceServerConfig] = []
    for raw_server in raw_ice_servers:
        if not isinstance(raw_server, Mapping):
            _invalid("invalid ICE server")

        raw_urls = raw_server.get("urls", _MISSING)
        urls = _normalise_ack_urls(raw_urls)
        username = _optional_ack_text(raw_server.get("username", None))
        credential = _optional_ack_text(raw_server.get("credential", None))
        credential_type = raw_server.get("credentialType", "password")
        servers.append(
            IceServerConfig(
                urls=urls,
                username=username,
                credential=credential,
                credential_type=credential_type,
            )
        )
    return tuple(servers)


def _validate_ice_profile(
    profile: tuple[IceServerConfig, ...],
    policy: str,
) -> tuple[IceServerConfig | None, IceServerConfig | None]:
    """Validate one redacted profile and return its STUN/TURN members."""

    if type(profile) is not tuple or not 1 <= len(profile) <= 2:
        _invalid("invalid ICE profile")
    if any(not isinstance(server, IceServerConfig) for server in profile):
        _invalid("invalid ICE profile")
    if any(len(server.urls) != 1 for server in profile):
        _invalid("invalid ICE profile")

    schemes = tuple(_ice_url_scheme(server.urls[0]) for server in profile)
    if len(profile) == 1:
        server = profile[0]
        if schemes[0] == "stun":
            if policy != "all":
                _invalid("relay policy requires TURN")
            return server, None
        if schemes[0] in ("turn", "turns"):
            return None, server
        _invalid("invalid ICE profile")

    if schemes[0] != "stun" or schemes[1] not in ("turn", "turns"):
        _invalid("invalid ICE profile")
    return profile[0], profile[1]


def _ice_endpoint_identity(server: IceServerConfig) -> tuple[str, str | None, str | None]:
    """Return an in-memory dedupe key; callers must not log this value."""

    return (server.urls[0], server.username, server.credential)


def _ice_url_scheme(url: str) -> str:
    # IceServerConfig has already validated this URI.  Keep profile selection
    # lexical so it neither logs nor exposes parser diagnostics.
    return url.partition(":")[0]


def _turn_url_rank(url: str) -> int:
    """Order validated TURN URLs as UDP, TCP, then TLS/TCP."""

    scheme, _separator, remainder = url.partition(":")
    if scheme == "turn":
        _authority, _query_separator, query = remainder.partition("?")
        return 1 if query == "transport=tcp" else 0
    if scheme == "turns":
        return 2
    _invalid("invalid TURN ICE profile")


def _encode_server(server: IceServerConfig) -> dict[str, Any]:
    return {
        "urls": list(server.urls),
        "username": server.username,
        "credential": server.credential,
        "credentialType": server.credential_type,
    }


def _parse_wire_server(raw_server: Any) -> IceServerConfig:
    _require_exact_mapping(raw_server, _SERVER_KEYS, "invalid ICE server")
    raw_urls = raw_server["urls"]
    if type(raw_urls) is not list:
        _invalid("invalid ICE server URLs")
    if not raw_urls or len(raw_urls) > MAX_URLS_PER_ICE_SERVER:
        _invalid("invalid ICE server URL count")
    if type(raw_server["username"]) is not str and raw_server["username"] is not None:
        _invalid("invalid ICE username")
    if type(raw_server["credential"]) is not str and raw_server["credential"] is not None:
        _invalid("invalid ICE credential")
    if type(raw_server["credentialType"]) is not str:
        _invalid("invalid ICE credential type")
    return IceServerConfig(
        urls=tuple(raw_urls),
        username=raw_server["username"],
        credential=raw_server["credential"],
        credential_type=raw_server["credentialType"],
    )


def _normalise_ack_urls(value: Any) -> tuple[str, ...]:
    if type(value) is str:
        urls = (value,)
    elif isinstance(value, (list, tuple)):
        urls = tuple(value)
    else:
        _invalid("invalid ICE server URLs")
    if not urls or len(urls) > MAX_URLS_PER_ICE_SERVER:
        _invalid("invalid ICE server URL count")
    return urls


def _optional_ack_text(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        _invalid("invalid ICE text")
    return value


def _validate_server_values(
    urls: tuple[str, ...],
    username: str | None,
    credential: str | None,
    credential_type: str,
) -> None:
    if type(urls) is not tuple:
        _invalid("invalid ICE server URLs")
    if not urls or len(urls) > MAX_URLS_PER_ICE_SERVER:
        _invalid("invalid ICE server URL count")
    if type(credential_type) is not str or credential_type != "password":
        _invalid("unsupported ICE credential type")

    has_turn = False
    for url in urls:
        has_turn = _validate_ice_url(url) or has_turn
    _validate_optional_text(username, "invalid ICE username")
    _validate_optional_text(credential, "invalid ICE credential")
    if has_turn and (username is None or username == "" or credential is None or credential == ""):
        _invalid("TURN requires credentials")


def _server_has_turn(server: IceServerConfig) -> bool:
    # Server instances are validated in their own __post_init__, so this is a
    # small scheme-only check that does not render or otherwise expose a URL.
    return any(url.startswith(("turn:", "turns:")) for url in server.urls)


def _validate_ice_url(value: Any) -> bool:
    if type(value) is not str or not value:
        _invalid("invalid ICE URL")
    if _utf8_length(value, "invalid ICE URL") > MAX_ICE_TEXT_BYTES:
        _invalid("ICE URL is too long")
    if _has_control_char(value) or any(character.isspace() for character in value):
        _invalid("invalid ICE URL")

    scheme, separator, remainder = value.partition(":")
    if not separator or scheme not in ("stun", "turn", "turns"):
        _invalid("unsupported ICE URL scheme")
    if not remainder or any(character in remainder for character in ("@", "/", "\\", "#")):
        _invalid("invalid ICE URL")

    authority, query_separator, query = remainder.partition("?")
    if not authority or (query_separator and (not query or "?" in query)):
        _invalid("invalid ICE URL")
    _validate_ice_authority(authority)
    if scheme == "stun":
        if query_separator:
            _invalid("unsupported STUN URL")
    elif query_separator and query not in ("transport=udp", "transport=tcp"):
        _invalid("unsupported TURN transport")

    # aiortc remains the source of truth for the URI syntax it will consume.
    try:
        from aiortc.rtcicetransport import parse_stun_turn_uri

        parsed = parse_stun_turn_uri(value)
    except (ImportError, TypeError, ValueError):
        _invalid("invalid ICE URL")
    if parsed.get("scheme") != scheme:
        _invalid("invalid ICE URL")
    transport = parsed.get("transport")
    if scheme == "turn" and transport not in ("udp", "tcp"):
        _invalid("unsupported TURN transport")
    if scheme == "turns" and transport != "tcp":
        _invalid("unsupported TURN transport")
    return scheme in ("turn", "turns")


def _validate_ice_authority(authority: str) -> None:
    host, separator, port = authority.rpartition(":")
    if separator:
        if not host or ":" in host or not port:
            _invalid("invalid ICE URL")
        if not all("0" <= character <= "9" for character in port):
            _invalid("invalid ICE port")
        try:
            port_number = int(port)
        except ValueError:
            _invalid("invalid ICE port")
        if not 1 <= port_number <= 65535:
            _invalid("invalid ICE port")
    else:
        host = authority
    if not host or any(character in host for character in ("@", "/", "\\", "#", "?", ":")):
        _invalid("invalid ICE URL")


def _validate_optional_text(value: str | None, error: str) -> None:
    if value is None:
        return
    if type(value) is not str:
        _invalid(error)
    if _utf8_length(value, error) > MAX_ICE_TEXT_BYTES or _has_control_char(value):
        _invalid(error)


def _validate_generation(value: Any) -> None:
    if type(value) is not int or not 0 <= value <= MAX_ICE_GENERATION:
        _invalid("invalid ICE generation")


def _validate_config_id(value: Any) -> None:
    if type(value) is not str:
        _invalid("invalid ICE config ID")
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        _invalid("invalid ICE config ID")
    if parsed.version != 4 or str(parsed) != value:
        _invalid("invalid ICE config ID")


def _validate_policy(value: Any) -> None:
    if type(value) is not str or value not in _POLICIES:
        _invalid("invalid ICE policy")


def _require_exact_mapping(value: Any, expected_keys: frozenset[str], error: str) -> None:
    if not isinstance(value, Mapping):
        _invalid(error)
    try:
        keys = frozenset(value.keys())
    except (AttributeError, TypeError):
        _invalid(error)
    if keys != expected_keys:
        _invalid(error)


def _utf8_length(value: str, error: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        _invalid(error)


def _has_control_char(value: str) -> bool:
    return any(
        ord(character) <= 0x1F
        or ord(character) == 0x7F
        or unicodedata.category(character) == "Cc"
        for character in value
    )


def _invalid(message: str) -> None:
    # Suppress parser contexts as well as keeping this message value-free:
    # malformed URI/UUID implementations sometimes include their input in a
    # ValueError string.
    raise IceBootstrapError(message) from None


__all__ = [
    "EXTENSION_KEY",
    "ICE_BOOTSTRAP_VERSION",
    "MAX_ICE_SERVERS",
    "MAX_URLS_PER_ICE_SERVER",
    "MAX_ICE_TEXT_BYTES",
    "MAX_ICE_GENERATION",
    "MAX_ICE_PROFILES",
    "IceBootstrapError",
    "IceBootstrapStatus",
    "IceAnswerStatus",
    "IceServerConfig",
    "IceBootstrap",
    "IceProfilePlan",
    "IceAnswer",
    "build_ice_profile_plan",
    "encode_ice_bootstrap",
    "parse_ice_bootstrap",
    "to_aiortc_ice_servers",
    "build_ice_answer",
    "encode_ice_answer",
    "build_ice_answer_extension",
    "parse_ice_answer",
    "try_parse_ice_answer",
    "validate_ice_answer_echo",
]

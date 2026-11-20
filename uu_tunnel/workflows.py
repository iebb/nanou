"""Explicit account and secure guest room workflows."""

from __future__ import annotations

import asyncio
import random
from collections import deque
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Awaitable, Callable

from .access import (
    CONNECT_TYPE_BY_CONFIRMATION,
    CONNECT_TYPE_PASSWORD_CONFIRMATION,
    generate_access_code,
    make_access_proof,
    validate_product_access_code,
)
from .api import (
    RoomConfig,
    UuApiClient,
    UuApiCommittedResponseError,
    UuApiError,
    UuApiTransientError,
    UuApiUnknownOutcomeError,
    UuAuthExpiredError,
    extract_room_config,
)
from .webrtc import TunnelSession, TunnelSessionError

Report = Callable[[str], None]
ApprovalRequest = Callable[[str, str], Awaitable[bool]]

ASSIST_POLL_INTERVAL_S = 3.0
ROOM_RETRY_BASE_S = 1.0
ROOM_RETRY_CAP_S = 30.0
API_COMMIT_COMPLETION_TIMEOUT_S = 35.0
ROOM_CLEANUP_DEADLINE_S = 30.0
_PENDING_ASSIST_CODE = 1135
_DEVICE_ROOM_NOT_READY_CODE = 2001
_GUEST_REQUEST_QUEUE_SIZE = 8
_GUEST_ID_TRACK_LIMIT = _GUEST_REQUEST_QUEUE_SIZE * 2
_GUEST_REQUEST_PASSWORD = "password"
_GUEST_REQUEST_CONFIRMATION = "confirmation"
_CONFIRMATION_CONTROL_MODES = frozenset(
    {CONNECT_TYPE_BY_CONFIRMATION, CONNECT_TYPE_PASSWORD_CONFIRMATION}
)


class _WorkflowStopped(Exception):
    """Internal control flow for an operator-requested, clean shutdown."""


class _GuestRequestCanceled(Exception):
    """The selected guest assistance request was canceled by the remote side."""


@dataclass(frozen=True)
class _GuestAccessRequest:
    """One inbound guest request emitted by a password or confirmation push."""

    kind: str
    control_id: str
    value: str


@dataclass
class _GuestDecisionState:
    """The confirmation request currently allowed to hold a SOAC offer."""

    control_id: str | None = None


class _AccountRoomState(Enum):
    """Local certainty about an account device-scoped room create request."""

    NOT_STARTED = auto()
    IN_FLIGHT = auto()
    COMMITTED = auto()
    UNKNOWN = auto()


def _safe_report(report: Report, message: str) -> None:
    try:
        report(message)
    except Exception:
        pass


async def _answer_guest_control_mode(
    guest_client: UuApiClient,
    control_id: str,
    control_mode: str,
    stop_event: asyncio.Event,
    report: Report,
) -> None:
    """Answer one get_control_mode push; a failed answer never breaks the room."""
    try:
        await _await_api_task(
            asyncio.create_task(
                guest_client.upload_guest_control_mode(control_id, control_mode)
            ),
            stop_event,
        )
    except (asyncio.CancelledError, _WorkflowStopped):
        raise
    except Exception:  # noqa: BLE001
        _safe_report(
            report,
            f"control mode answer failed for [{_short_control_id(control_id)}]",
        )


async def _guest_request_worker(
    requests: asyncio.Queue[_GuestAccessRequest],
    pending_password_ids: set[str],
    canceled_confirmation_ids: set[str],
    confirmation_cancel_events: dict[str, asyncio.Event],
    guest_client: UuApiClient,
    session: TunnelSession,
    access_code: str,
    approve_request: ApprovalRequest,
    auto_approve: bool,
    decision_state: _GuestDecisionState,
    stop_event: asyncio.Event,
    report: Report,
) -> None:
    """Serialize both guest entry modes until exactly one is authorized."""

    while True:
        request, cancel_event = await _next_guest_request(
            requests,
            canceled_confirmation_ids,
            confirmation_cancel_events,
            session,
            stop_event,
        )
        try:
            authorized = await _route_guest_request(
                request,
                guest_client=guest_client,
                session=session,
                access_code=access_code,
                approve_request=approve_request,
                auto_approve=auto_approve,
                stop_event=stop_event,
                report=report,
                cancel_event=cancel_event,
                decision_state=decision_state,
            )
        finally:
            if request.kind == _GUEST_REQUEST_PASSWORD:
                pending_password_ids.discard(request.control_id)
            elif request.kind == _GUEST_REQUEST_CONFIRMATION:
                confirmation_cancel_events.pop(request.control_id, None)
        if authorized:
            # mark_authorized() is irreversible for this session.  Returning
            # here leaves any later queue entries unaccepted and guarantees
            # one active controller per guest room.
            return


async def _route_guest_request(
    request: _GuestAccessRequest,
    *,
    guest_client: UuApiClient,
    session: TunnelSession,
    access_code: str,
    approve_request: ApprovalRequest,
    auto_approve: bool,
    stop_event: asyncio.Event,
    report: Report,
    cancel_event: asyncio.Event | None,
    decision_state: _GuestDecisionState,
) -> bool:
    """Run the matching proof or confirmation decision for one pushed request."""

    if request.kind == _GUEST_REQUEST_PASSWORD:
        return await _handle_password_guest_request(
            request,
            guest_client=guest_client,
            session=session,
            access_code=access_code,
            stop_event=stop_event,
            report=report,
        )
    if request.kind == _GUEST_REQUEST_CONFIRMATION:
        if cancel_event is None:
            raise TunnelSessionError("confirmation request is missing cancellation state")
        return await _handle_confirmation_guest_request(
            request,
            guest_client=guest_client,
            session=session,
            approve_request=approve_request,
            auto_approve=auto_approve,
            stop_event=stop_event,
            report=report,
            cancel_event=cancel_event,
            decision_state=decision_state,
        )
    raise TunnelSessionError("guest request type is invalid")


async def _handle_password_guest_request(
    request: _GuestAccessRequest,
    *,
    guest_client: UuApiClient,
    session: TunnelSession,
    access_code: str,
    stop_event: asyncio.Event,
    report: Report,
) -> bool:
    """Submit a password proof, opening authorization only on known success."""

    try:
        proof = make_access_proof(request.value, access_code)
        # A proof request is the sole point at which this password guest may
        # retain an inbound offer.  Do not open the window while waiting for a
        # push or constructing its proof.
        try:
            session.begin_authorization_decision()
            submitted = await _await_api_task(
                asyncio.create_task(
                    guest_client.submit_guest_access_proof(request.control_id, proof)
                ),
                stop_event,
                commit_sensitive=True,
            )
            if submitted is None or stop_event.is_set():
                raise _WorkflowStopped()
            session.mark_authorized()
        finally:
            # Definite failure, committed/unknown outcome, and owner
            # cancellation all return the guest to idle before control leaves
            # this request. Authorized sessions are never revoked.
            session.cancel_authorization_decision()
    except UuAuthExpiredError:
        raise
    except UuApiCommittedResponseError:
        # The proof endpoint committed but did not return a usable result;
        # without a guest clear operation this room must terminate.
        raise
    except UuApiUnknownOutcomeError:
        # A proof may have been accepted remotely. Do not try another proof
        # for this control ID or infer a compensating decision.
        raise
    except UuApiError:
        report(f"access proof failed for [{_short_control_id(request.control_id)}]")
        return False
    except ValueError:
        report(f"access proof rejected for [{_short_control_id(request.control_id)}]")
        return False
    report(f"access proof submitted for [{_short_control_id(request.control_id)}]")
    return True


async def _handle_confirmation_guest_request(
    request: _GuestAccessRequest,
    *,
    guest_client: UuApiClient,
    session: TunnelSession,
    approve_request: ApprovalRequest,
    auto_approve: bool,
    stop_event: asyncio.Event,
    report: Report,
    cancel_event: asyncio.Event,
    decision_state: _GuestDecisionState,
) -> bool:
    """Prompt or auto-approve one ID-only request with a bounded SOAC window."""

    name = request.value
    if cancel_event.is_set():
        raise _GuestRequestCanceled()
    if auto_approve:
        approved = True
    else:
        try:
            approved = await _await_session_task(
                session,
                asyncio.create_task(approve_request(name, request.control_id)),
                stop_event,
                cancel_event=cancel_event,
            )
        except (_GuestRequestCanceled, _WorkflowStopped, UuAuthExpiredError):
            raise
        except UuApiUnknownOutcomeError:
            raise
        except Exception:
            if cancel_event.is_set():
                raise _GuestRequestCanceled()
            report(f"local approval failed for [{name}]; rejecting")
            await _await_guest_decision(
                asyncio.create_task(guest_client.reject_guest_assist(request.control_id)),
                stop_event,
                cancel_event,
            )
            return False

    if approved is not True:
        await _await_guest_decision(
            asyncio.create_task(guest_client.reject_guest_assist(request.control_id)),
            stop_event,
            cancel_event,
        )
        report(f"assistance rejected for [{name}]")
        return False

    # Keep callback thinking and every rejection in the idle state. Only the
    # non-idempotent approval request gets a bounded SOAC quarantine window.
    if cancel_event.is_set():
        raise _GuestRequestCanceled()
    decision_state.control_id = request.control_id
    try:
        session.begin_authorization_decision()
        await _await_guest_decision(
            asyncio.create_task(guest_client.approve_guest_assist(request.control_id)),
            stop_event,
            cancel_event,
        )
        # _await_guest_decision performs a cancellation recheck after the
        # known commit result. Keep this immediate check adjacent to
        # authorization as well.
        if cancel_event.is_set():
            raise _GuestRequestCanceled()
        session.mark_authorized()
    finally:
        # On API failure, unknown outcome, local stop, or a remote
        # cancellation, this closes the window before the exception leaves
        # this request. Once marked it is a harmless non-deauthorizing no-op.
        decision_state.control_id = None
        session.cancel_authorization_decision()
    report(f"assistance approved for [{name}]")
    return True


async def _await_guest_negotiation(
    negotiate_task: asyncio.Task[Any],
    worker_task: asyncio.Task[Any],
) -> Any:
    """Wait for one router authorization and the resulting controlled offer."""

    done, _pending = await asyncio.wait(
        {negotiate_task, worker_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if worker_task in done:
        try:
            await worker_task
        except (
            _GuestRequestCanceled,
            _WorkflowStopped,
            UuApiError,
            TunnelSessionError,
        ):
            raise
        except Exception:  # noqa: BLE001
            raise TunnelSessionError("guest request worker failed") from None
        # A successful worker has just opened the controlled authorization
        # gate. Let the one negotiation task wait for the fresh accepted offer
        # instead of treating normal worker completion as an error.
        return await negotiate_task

    try:
        return await negotiate_task
    finally:
        await _await_owner_shielded(
            _cancel_and_drain_tasks(negotiate_task, worker_task)
        )


async def _next_guest_request(
    requests: asyncio.Queue[_GuestAccessRequest],
    canceled_confirmation_ids: set[str],
    confirmation_cancel_events: dict[str, asyncio.Event],
    session: TunnelSession,
    stop_event: asyncio.Event,
) -> tuple[_GuestAccessRequest, asyncio.Event | None]:
    """Read the next non-canceled password or confirmation request."""

    while True:
        request = await _await_session_task(
            session,
            asyncio.create_task(requests.get()),
            stop_event,
            stop_on_cancellation=False,
        )
        if request.kind != _GUEST_REQUEST_CONFIRMATION:
            return request, None
        cancel_event = confirmation_cancel_events.setdefault(
            request.control_id,
            asyncio.Event(),
        )
        if request.control_id in canceled_confirmation_ids or cancel_event.is_set():
            confirmation_cancel_events.pop(request.control_id, None)
            continue
        return request, cancel_event


async def _await_api_task(
    task: asyncio.Task[Any],
    stop_event: asyncio.Event,
    *,
    commit_sensitive: bool = False,
    cancel_event: asyncio.Event | None = None,
) -> Any | None:
    """Race an API task against local stop without replaying a commit.

    Idempotent calls may be canceled as soon as the operator stops.  Once a
    non-idempotent task was started, cancellation cannot establish that the
    remote side did not commit it.  In that case the task stays shielded until
    the bounded API completion window resolves to a result, a classified API
    error, or an explicit unknown-outcome error.
    """

    stop_wait = asyncio.create_task(stop_event.wait())
    cancel_wait = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
    try:
        waiters: set[asyncio.Future[Any]] = {task, stop_wait}
        if cancel_wait is not None:
            waiters.add(cancel_wait)
        done, _pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return await task
        if commit_sensitive:
            return await _await_commit_sensitive_task(task)
        await _cancel_and_drain_task(task)
        return None
    except asyncio.CancelledError:
        if commit_sensitive:
            await _await_commit_sensitive_task(task)
        else:
            await _cancel_and_drain_task(task)
        raise
    finally:
        await _cancel_event_wait(stop_wait)
        if cancel_wait is not None:
            await _cancel_event_wait(cancel_wait)


async def _await_commit_sensitive_task(task: asyncio.Task[Any]) -> Any:
    """Await a started commit without allowing owner cancellation to cancel it."""

    deadline = asyncio.get_running_loop().time() + API_COMMIT_COMPLETION_TIMEOUT_S
    cancelled = False
    while not task.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            _observe_background_task(task)
            raise UuApiUnknownOutcomeError(
                "UU API request outcome unknown: commit completion timed out"
            )
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except asyncio.CancelledError:
            # The caller's cancellation is deferred until the operation has a
            # known bounded outcome.  The task itself remains live.
            cancelled = True
            continue
        except asyncio.TimeoutError:
            _observe_background_task(task)
            raise UuApiUnknownOutcomeError(
                "UU API request outcome unknown: commit completion timed out"
            ) from None

    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


def _observe_background_task(task: asyncio.Task[Any]) -> None:
    """Consume a late bounded-commit exception if a broken client outlives it."""

    def consume(completed: asyncio.Task[Any]) -> None:
        try:
            completed.exception()
        except asyncio.CancelledError:
            pass

    task.add_done_callback(consume)


async def _await_guest_decision(
    task: asyncio.Task[Any],
    stop_event: asyncio.Event,
    cancel_event: asyncio.Event,
) -> Any:
    """Finish an approve/reject request before honoring local cancellation."""

    result = await _await_api_task(
        task,
        stop_event,
        commit_sensitive=True,
        cancel_event=cancel_event,
    )
    if cancel_event.is_set():
        raise _GuestRequestCanceled()
    if result is None or stop_event.is_set():
        raise _WorkflowStopped()
    return result


async def _await_session_task(
    session: TunnelSession,
    task: asyncio.Task[Any],
    stop_event: asyncio.Event,
    *,
    cancel_event: asyncio.Event | None = None,
    stop_on_cancellation: bool = True,
    stop_session_on_abort: bool = True,
) -> Any:
    """Await a phase, distinguishing operator stop from room termination."""
    stop_wait = asyncio.create_task(stop_event.wait())
    closed_wait = asyncio.create_task(session.wait_closed())
    cancel_wait = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
    try:
        waiters: set[asyncio.Future[Any]] = {task, stop_wait, closed_wait}
        if cancel_wait is not None:
            waiters.add(cancel_wait)
        done, _pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        if cancel_wait is not None and (cancel_event.is_set() or cancel_wait in done):
            await _abort_session_task(session, task, stop_session_on_abort)
            raise _GuestRequestCanceled()
        if task in done:
            return await task
        if stop_wait in done:
            await _abort_session_task(session, task, stop_session_on_abort)
            raise _WorkflowStopped()
        error = _closed_error(closed_wait.result())
        await _abort_session_task(session, task, stop_session_on_abort)
        raise error
    except asyncio.CancelledError:
        if stop_on_cancellation:
            await _abort_session_task(session, task, stop_session_on_abort)
        else:
            await _cancel_and_drain_task(task)
        raise
    finally:
        await _cancel_event_wait(stop_wait)
        await _cancel_event_wait(closed_wait)
        if cancel_wait is not None:
            await _cancel_event_wait(cancel_wait)


async def _abort_session_task(
    session: TunnelSession,
    task: asyncio.Task[Any],
    stop_session: bool,
) -> None:
    if stop_session:
        await _stop_and_drain(session, task)
    else:
        await _cancel_and_drain_task(task)


async def _stop_and_drain(session: TunnelSession, task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    try:
        await _stop_session_owner_shielded(session)
    finally:
        await _cancel_and_drain_task(task)


async def _cancel_and_drain_task(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def _cancel_and_drain_tasks(
    *tasks: asyncio.Task[Any] | None,
) -> None:
    """Cancel every supplied task before draining either of them."""

    pending: list[asyncio.Task[Any]] = []
    for task in tasks:
        if task is None or task in pending:
            continue
        pending.append(task)
        if not task.done():
            task.cancel()
    for task in pending:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass


async def _await_owner_shielded(awaitable: Awaitable[Any]) -> Any:
    """Complete cleanup even when the workflow owner is canceled.

    The cancellation is re-raised after the owned task finishes.  Callers use
    nested ``finally`` blocks when more than one cleanup action is mandatory,
    ensuring (for example) session.stop precedes account clear_room.
    """

    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _stop_session_owner_shielded(session: TunnelSession) -> None:
    await _await_owner_shielded(session.stop())


async def _wait_stop(stop_event: asyncio.Event, timeout_s: float) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout_s)
        return True
    except asyncio.TimeoutError:
        return False


async def _release_account_room(
    client: UuApiClient,
    stop_event: asyncio.Event,
    report: Report,
) -> bool:
    # Cleanup deliberately does not honor stop_event: a committed or unknown
    # account create must be reconciled before a future create could overlap
    # the same device-scoped room.  A total deadline prevents shutdown from
    # waiting forever on an unavailable cleanup endpoint.
    del stop_event
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ROOM_CLEANUP_DEADLINE_S
    failure_count = 0
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            report("room cleanup deadline exceeded; continuing shutdown")
            return False
        try:
            await asyncio.wait_for(client.clear_room(), timeout=remaining)
        except asyncio.TimeoutError:
            report("room cleanup deadline exceeded; continuing shutdown")
            return False
        except UuAuthExpiredError:
            raise
        except UuApiUnknownOutcomeError:
            raise
        except UuApiTransientError:
            report("room cleanup transient failure; retrying")
            delay = _full_jitter_delay(failure_count, cap_s=ROOM_RETRY_CAP_S)
            remaining = deadline - loop.time()
            if remaining <= 0:
                report("room cleanup deadline exceeded; continuing shutdown")
                return False
            await asyncio.sleep(min(delay, remaining))
            failure_count += 1
            continue
        except UuApiError:
            report("room cleanup failed")
            return False
        else:
            return True


async def _own_controller_device_id(
    client: UuApiClient,
    stop_event: asyncio.Event,
) -> str | None:
    """Resolve the account controller's own registered device exactly once."""

    value = await _await_api_task(
        asyncio.create_task(client.ensure_device()),
        stop_event,
    )
    if value is None:
        return None
    return _required_id(value, "controller_device_id")


def _full_jitter_delay(
    failure_count: int,
    *,
    cap_s: float = ROOM_RETRY_CAP_S,
) -> float:
    """Return exponential full jitter with a bounded, non-secret schedule."""

    if failure_count < 0:
        raise ValueError("failure_count must not be negative")
    # Clamp the exponent before integer growth becomes needlessly enormous for
    # a long-lived host process; the cap makes all larger values equivalent.
    ceiling = min(cap_s, ROOM_RETRY_BASE_S * (2 ** min(failure_count, 30)))
    return random.uniform(0.0, ceiling)


def _remember_id(ids: set[str], order: deque[str], value: str) -> None:
    if value in ids:
        return
    if len(order) >= _GUEST_ID_TRACK_LIMIT:
        ids.discard(order.popleft())
    ids.add(value)
    order.append(value)


async def _cancel_event_wait(task: asyncio.Task[Any]) -> None:
    if task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _closed_error(value: Any) -> TunnelSessionError:
    return value if isinstance(value, TunnelSessionError) else TunnelSessionError("会话已关闭")


def _safe_remote_name(value: str) -> str:
    name = "".join(character for character in str(value) if character.isprintable())[:64]
    return name or "?"


def _short_control_id(value: str) -> str:
    return value if len(value) <= 12 else f"{value[:8]}…{value[-4:]}"


def _validated_access_code(value: str) -> str:
    try:
        return validate_product_access_code(value)
    except ValueError as exc:
        raise UuApiError("access code 格式无效") from exc


def _required_id(value: str, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise UuApiError(f"{name} 不能为空")
    return value


def _require_room_config(upstream: dict[str, Any]) -> RoomConfig:
    room_config = extract_room_config(upstream)
    if room_config is None:
        raise UuApiError("响应中缺少 room_config")
    return room_config

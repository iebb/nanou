"""UU API 异步客户端：设备注册（桌面端）、登录、房间创建/加入、设备列表。

设备注册走桌面端路径 /api/v1/device/windows/init，
使本机在 UU 侧表现为一台"可被连接的 Windows 设备"。
"""

from __future__ import annotations

import asyncio
import json
import unicodedata
from typing import Any, TypedDict

import aiohttp

from ..access import (
    CONNECT_TYPE_BY_CONFIRMATION,
    CONNECT_TYPE_BY_PASSWORD,
    CONNECT_TYPE_PASSWORD_CONFIRMATION,
    validate_access_code,
)
from ..profile import generate_device_profile
from .room import extract_room_config
from .signing import (
    PATH_CLEAR_BY_DEVICE,
    PATH_DEVICE_LIST,
    PATH_DEVICE_INIT,
    PATH_GUEST_CREATE,
    PATH_GUEST_ROOM_CREATE,
    PATH_GUEST_SHARE_CONFIRMATION,
    PATH_GUEST_SHARE_UPLOAD_SIGN,
    PATH_GUEST_UPLOAD_CONTROL_MODE,
    PATH_JOIN_BY_CODE,
    PATH_JOIN_BY_CONFIRMATION,
    PATH_JOIN_BY_DEVICE,
    PATH_MOBILE_CODE,
    PATH_MOBILE_LOGIN,
    PATH_ROOM_CREATE,
    PATH_ROOM_CONTROL_MODE,
    PATH_GUEST_SHARE_INFO,
    API_BASE,
    RequestSigner,
)


class UuApiError(RuntimeError):
    """上游返回非零 code 或网络/格式错误。"""


class UuApiResponseError(UuApiError):
    """UU returned a valid, nonzero business response code."""

    def __init__(
        self,
        code: int,
        msg: Any = None,
        *,
        operation: str | None = None,
    ) -> None:
        self.code = code
        self.msg = _sanitize_response_msg(msg)
        label = "UU API 错误" if operation is None else f"UU API {operation} 错误"
        detail = f"{label} code={code}"
        if self.msg is not None:
            detail = f"{detail} msg={self.msg!r}"
        super().__init__(detail)


class UuApiTransientError(UuApiError):
    """幂等操作遇到可安全重试的暂时性传输或服务端故障。"""


class UuApiUnknownOutcomeError(UuApiError):
    """非幂等操作中断，服务端是否已提交无法确定。"""


class UuApiCommittedResponseError(UuApiUnknownOutcomeError):
    """服务端已确认成功，但端点所需的结果字段不可用。"""


class UuAuthExpiredError(UuApiError):
    """UU 明确报告登录或游客身份已过期（code=1120）。"""


class DeviceRecord(TypedDict):
    """设备列表中的可连接目标。"""

    device_id: str
    alias: str
    platform: int | str | None
    online: bool
    can_be_controlled: bool
    occupied: bool
    occupied_by: list[str]
    shared: bool
    raw: dict[str, Any]


class UuApiClient:
    """带登录态的 UU API 客户端（桌面端身份）。

    登录态（token/user_id/client_id/device_id）由调用方持久化（见 uu_tunnel.auth）。
    """

    def __init__(self, state: dict[str, Any] | None = None, timeout: float = 30.0):
        self.state: dict[str, Any] = state if state is not None else {}
        self._normalize_identity_state()
        self._signer = RequestSigner(self.state)
        self._session: aiohttp.ClientSession | None = None
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._pending_device_init: tuple[dict[str, Any], str, str] | None = None
        self._device_list_cache: list[DeviceRecord] | None = None

    @property
    def logged_in(self) -> bool:
        return bool(self.state.get("token") and self.state.get("user_id"))

    @property
    def is_guest(self) -> bool:
        return bool(self.state.get("token") and self.state.get("guest_id"))

    @property
    def device_id(self) -> str:
        return str(self.state.get("device_id") or "")

    def require_account_identity(self) -> None:
        """Raise before an account-only API operation reaches the network."""
        if not self.logged_in:
            raise UuApiError("需要已登录账号：先执行 login")

    def require_guest_identity(self) -> None:
        """Raise unless this client owns a current in-memory guest identity."""
        if not self.is_guest:
            raise UuApiError("需要游客身份：先创建游客会话")

    def export_state(self) -> dict[str, Any]:
        return dict(self.state)

    def clear_credentials(self) -> None:
        """Drop account/guest credentials while retaining this device identity."""
        for key in ("token", "user_id", "guest_id"):
            self.state.pop(key, None)
        self._device_list_cache = None
        self._refresh_signer()

    def set_device_name(self, name: str) -> None:
        """保存本地名称，供下一次设备注册使用。"""
        self.state["device_name"] = _normalize_device_name(name)

    async def set_device_alias(self, alias: str) -> dict:
        """立即更新当前设备别名，并返回响应中的 data。"""
        device_id = self.device_id
        if not device_id:
            raise UuApiError("当前身份缺少 device_id，无法更新设备名称")
        self.require_account_identity()
        normalized = _normalize_device_name(alias)
        payload = await self._request(
            "PUT",
            f"/api/v1/device/{device_id}",
            {"alias": normalized},
            idempotent=True,
            operation="set-device-alias",
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise _committed_response_error("set-device-alias")
        self.state["device_name"] = normalized
        return data

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        idempotent: bool,
        operation: str,
        raw: bool = False,
        _identity_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Low-level signed request for identity bootstrap and guarded methods.

        ``operation`` must be a short static label chosen by the caller; it is
        the only request-specific value included in transport error messages.
        Cancellation deliberately propagates. Callers must shield
        commit-sensitive non-idempotent tasks and reconcile their result.
        """

        request_state = self.state if _identity_state is None else _identity_state
        signer = self._signer if _identity_state is None else RequestSigner(request_state)
        body_text = "" if body is None else json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        headers = signer.common_headers()
        guest_id = request_state.get("guest_id")
        if guest_id:
            headers["X-Param-Guest-ID"] = str(guest_id)
        headers["X-Param-SIGN"] = signer.sign(method, path, headers, body_text)
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        if request_state.get("token"):
            headers["Authorization"] = f"Bearer {request_state['token']}"

        try:
            async with self._session_get().request(
                method,
                f"{API_BASE}{path}",
                headers=headers,
                data=body_text.encode() if body is not None else None,
            ) as resp:
                raw = await resp.content.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise _malformed_success_error(
                        idempotent=idempotent,
                        operation=operation,
                        detail="响应体过大",
                    )
                text = raw.decode(resp.charset or "utf-8", errors="replace")
                if not 200 <= resp.status < 300:
                    if resp.status in (408, 429) or 500 <= resp.status < 600:
                        raise _outcome_error(idempotent=idempotent, operation=operation)
                    body = _sanitize_response_msg(
                        text[:_HTTP_ERROR_BODY_SNIPPET_LENGTH]
                    ) or "<empty response body>"
                    raise UuApiError(f"UU API {operation} HTTP {resp.status}: {body}")
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            raise _outcome_error(idempotent=idempotent, operation=operation) from None
        except aiohttp.ClientError:
            raise _outcome_error(idempotent=idempotent, operation=operation) from None

        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            raise _malformed_success_error(
                idempotent=idempotent,
                operation=operation,
                detail="返回非 JSON",
            ) from None
        if not isinstance(payload, dict):
            raise _malformed_success_error(
                idempotent=idempotent,
                operation=operation,
                detail="返回结构异常",
            )
        code = payload.get("code")
        if not isinstance(code, int) or isinstance(code, bool):
            raise _malformed_success_error(
                idempotent=idempotent,
                operation=operation,
                detail="响应缺少有效 code",
            )
        if code == 1120:
            raise UuAuthExpiredError(f"UU API {operation} 身份已过期 (code=1120)")
        if code != 0 and not raw:
            raise UuApiResponseError(
                code,
                payload.get("msg"),
                operation=operation,
            )
        return payload

    def _normalize_identity_state(self) -> None:
        for key in ("client_id", "device_id", "system_id", "user_id", "guest_id", "token"):
            value = self.state.get(key)
            if value is not None:
                self.state[key] = str(value)
        if self.state.get("token") and "system_id" not in self.state:
            system_id = _jwt_field(str(self.state["token"]), "system_id")
            if system_id is not None:
                self.state["system_id"] = str(system_id)

    def _refresh_signer(self) -> None:
        self._normalize_identity_state()
        self._signer = RequestSigner(self.state)

    async def ensure_device(self) -> str:
        """设备注册（幂等）。返回 device_id 并写入登录态。

        注册为"可被连接的 Windows 设备"（controllable=true），
        注册后 UU 侧设备列表可见本机，其他客户端可对其发起连接。
        """
        if self.state.get("device_id"):
            return str(self.state["device_id"])

        # The initial device-init request is itself signed with the generated
        # stable identity. Keep that identity staged until its device_id is
        # fully validated, so a malformed success cannot alter local state.
        pending = self._pending_device_init
        if pending is None:
            profile = generate_device_profile(
                client_id=str(self.state.get("client_id") or "") or None,
                name=self.state.get("device_name") or None,
            )
            client_id = _required_local_identity_value(
                profile.get("client_id"),
                field_label="client_id",
                max_length=_MAX_IDENTITY_FIELD_LENGTH,
            )
            system_id = _required_local_identity_value(
                profile.get("system_id"),
                field_label="system_id",
                max_length=_MAX_IDENTITY_FIELD_LENGTH,
            )
            self._pending_device_init = (profile, client_id, system_id)
        else:
            profile, client_id, system_id = pending
            client_id = _required_local_identity_value(
                client_id,
                field_label="client_id",
                max_length=_MAX_IDENTITY_FIELD_LENGTH,
            )
            system_id = _required_local_identity_value(
                system_id,
                field_label="system_id",
                max_length=_MAX_IDENTITY_FIELD_LENGTH,
            )
        init_state = dict(self.state)
        init_state["client_id"] = client_id
        init_state["system_id"] = system_id
        payload = await self._request(
            "POST",
            PATH_DEVICE_INIT,
            profile,
            idempotent=True,
            operation="device-init",
            _identity_state=init_state,
        )
        device_id = _required_committed_value(
            payload,
            "device-init",
            field_label="device_id",
            max_length=_MAX_IDENTITY_FIELD_LENGTH,
        )
        next_state = dict(self.state)
        next_state["client_id"] = client_id
        next_state["system_id"] = system_id
        next_state["device_id"] = device_id
        self.state.clear()
        self.state.update(next_state)
        self._pending_device_init = None
        self._refresh_signer()
        return device_id

    async def send_sms_code(self, region_code: str, mobile: str) -> None:
        """请求短信验证码。"""
        await self.ensure_device()
        await self._request(
            "POST",
            PATH_MOBILE_CODE,
            {"country_code": region_code, "mobile": mobile, "type": "login"},
            idempotent=False,
            operation="send-sms-code",
        )

    async def login_by_mobile(self, region_code: str, mobile: str, code: str) -> None:
        """短信验证码登录；token/user_id 写入登录态。"""
        await self.ensure_device()
        payload = await self._request(
            "POST",
            PATH_MOBILE_LOGIN,
            {"country_code": region_code, "mobile": mobile, "code": code},
            idempotent=False,
            operation="mobile-login",
        )
        token = _required_committed_value(
            payload,
            "mobile-login",
            field_label="token",
            max_length=_MAX_TOKEN_LENGTH,
        )
        user_id = _required_committed_value(
            payload,
            "mobile-login",
            field_label="user_id",
            max_length=_MAX_IDENTITY_FIELD_LENGTH,
        )
        self._apply_login_token(token, user_id, operation="mobile-login")

    def _apply_login_token(
        self,
        token: str,
        user_id: str,
        *,
        operation: str = "mobile-login",
    ) -> None:
        """登录 token 写入状态；身份头切换为 JWT 绑定的 device/client。"""
        if (
            not _is_strict_identity_value(token, max_length=_MAX_TOKEN_LENGTH)
            or not _is_strict_identity_value(user_id, max_length=_MAX_IDENTITY_FIELD_LENGTH)
        ):
            raise _committed_response_error(operation)
        jwt_device = _optional_committed_value(
            _jwt_field(token, "device_id"),
            operation,
            field_label="device_id",
            max_length=_MAX_IDENTITY_FIELD_LENGTH,
        )
        jwt_client = _optional_committed_value(
            _jwt_field(token, "client_id"),
            operation,
            field_label="client_id",
            max_length=_MAX_IDENTITY_FIELD_LENGTH,
        )
        jwt_system = _optional_committed_value(
            _jwt_field(token, "system_id"),
            operation,
            field_label="system_id",
            max_length=_MAX_IDENTITY_FIELD_LENGTH,
        )
        next_state = dict(self.state)
        next_state["token"] = token
        next_state["user_id"] = user_id
        next_state.pop("guest_id", None)
        if jwt_device is not None:
            next_state["device_id"] = jwt_device
        if jwt_client is not None:
            next_state["client_id"] = jwt_client
        if jwt_system is not None:
            next_state["system_id"] = jwt_system
        self.state.clear()
        self.state.update(next_state)
        self._device_list_cache = None
        self._refresh_signer()

    async def create_room(self) -> dict[str, Any]:
        """被控端创建房间（POST /api/v1/room/create）。

        固定 body 为 {"last_controlled_interval": 0}。返回上游响应（含 room_config）。
        """
        self.require_account_identity()
        payload = await self._request(
            "POST",
            PATH_ROOM_CREATE,
            {"last_controlled_interval": 0},
            idempotent=False,
            operation="create-room",
        )
        _require_committed_room_config(payload, "create-room")
        return payload

    async def ensure_guest(self) -> None:
        """游客身份：guest/create 拿 token + guest_id 写入登录态。

        前置：设备已注册（ensure_device）。游客 token 用于 guest/room/* 等
        免登录路径，与账号登录互斥。
        """
        if self.logged_in:
            raise UuApiError("已登录账号不能切换为游客身份")
        if self.state.get("guest_id") and self.state.get("token"):
            return
        await self.ensure_device()
        payload = await self._request(
            "POST",
            PATH_GUEST_CREATE,
            None,
            idempotent=False,
            operation="create-guest",
        )
        token = _required_committed_value(
            payload,
            "create-guest",
            field_label="token",
            max_length=_MAX_TOKEN_LENGTH,
        )
        guest_id = _required_committed_value(
            payload,
            "create-guest",
            field_label="guest_id",
            max_length=_MAX_IDENTITY_FIELD_LENGTH,
        )
        next_state = dict(self.state)
        next_state["token"] = token
        next_state["guest_id"] = guest_id
        next_state.pop("user_id", None)
        self.state.clear()
        self.state.update(next_state)
        self._refresh_signer()

    async def create_guest_room(self) -> dict[str, Any]:
        """游客建房（免登录）。自动确保游客身份。"""
        await self.ensure_guest()
        self.require_guest_identity()
        payload = await self._request(
            "POST", PATH_GUEST_ROOM_CREATE,
            {"last_controlled_interval": 0},
            idempotent=False,
            operation="create-guest-room",
        )
        _require_committed_room_config(payload, "create-guest-room")
        return payload

    async def share_guest_room(self) -> str:
        """Publish the current guest room and return its non-empty connect_id."""
        self.require_guest_identity()
        payload = await self._request(
            "POST",
            PATH_GUEST_SHARE_INFO,
            None,
            idempotent=True,
            operation="guest-share-info",
        )
        return _required_committed_value(
            payload,
            "guest-share-info",
            field_label="connect_id",
            max_length=_MAX_CONNECT_ID_LENGTH,
        )

    async def approve_guest_assist(self, control_id: str) -> dict[str, Any]:
        """Approve one guest-room confirmation request without a password."""
        self.require_guest_identity()
        normalized_control_id = _normalize_required_id(control_id, "control_id")
        return await self._request(
            "POST",
            PATH_GUEST_SHARE_CONFIRMATION,
            {
                "control_id": normalized_control_id,
                "allow_control": True,
                "need_password": False,
            },
            idempotent=False,
            operation="approve-guest-assist",
        )

    async def reject_guest_assist(self, control_id: str) -> dict[str, Any]:
        """Reject one guest-room confirmation request without a password."""
        self.require_guest_identity()
        normalized_control_id = _normalize_required_id(control_id, "control_id")
        return await self._request(
            "POST",
            PATH_GUEST_SHARE_CONFIRMATION,
            {
                "control_id": normalized_control_id,
                "allow_control": False,
                "need_password": False,
            },
            idempotent=False,
            operation="reject-guest-assist",
        )

    async def submit_guest_access_proof(self, control_id: str, proof: str) -> dict[str, Any]:
        """Upload a by-password guest proof for one remote-control request."""
        self.require_guest_identity()
        normalized_control_id = _normalize_required_id(control_id, "control_id")
        validated_proof = _validate_access_proof(proof)
        return await self._request(
            "POST",
            PATH_GUEST_SHARE_UPLOAD_SIGN,
            {
                "can_remote_control": True,
                "control_id": normalized_control_id,
                "sign": validated_proof,
                "control_mode": CONNECT_TYPE_BY_PASSWORD,
                "need_confirmation": False,
            },
            idempotent=False,
            operation="submit-guest-access-proof",
        )

    async def upload_guest_control_mode(self, control_id: str, control_mode: str) -> dict[str, Any]:
        """Answer a get_control_mode push for one guest-room remote-control request."""
        self.require_guest_identity()
        normalized_control_id = _normalize_required_id(control_id, "control_id")
        if control_mode not in _ROOM_CONTROL_MODES:
            raise UuApiError("control_mode 取值无效")
        return await self._request(
            "POST",
            PATH_GUEST_UPLOAD_CONTROL_MODE,
            {
                "control_id": normalized_control_id,
                "allow_control": True,
                "control_mode": control_mode,
            },
            idempotent=False,
            operation="upload-guest-control-mode",
        )

    async def clear_room(self) -> dict[str, Any]:
        """退出账号设备房间；该接口不接受游客 token。"""
        self.require_account_identity()
        device_id = self.device_id
        if not device_id:
            raise UuApiError("当前身份缺少 device_id，无法清理房间")
        return await self._request(
            "POST",
            PATH_CLEAR_BY_DEVICE % device_id,
            {},
            idempotent=True,
            operation="clear-room",
        )

    async def list_devices(self) -> list[DeviceRecord]:
        """列出账号下除本机外的绑定与共享设备。"""
        self.require_account_identity()
        payload = await self._request(
            "GET",
            PATH_DEVICE_LIST,
            idempotent=True,
            operation="list-devices",
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            self._device_list_cache = []
            return []

        current = data.get("current_device")
        current_id = str(current.get("device_id") or "") if isinstance(current, dict) else ""
        devices: list[DeviceRecord] = []
        for key, shared in (
            ("my_binded_devices", False),
            ("others_shared_devices", True),
        ):
            group = data.get(key)
            if isinstance(group, list):
                for item in group:
                    if not isinstance(item, dict):
                        continue
                    device_id = str(item.get("device_id") or "")
                    if current_id and device_id == current_id:
                        continue
                    participants = item.get("participants_info")
                    occupied = isinstance(participants, list) and bool(participants)
                    occupied_by: list[str] = []
                    if isinstance(participants, list):
                        for participant in participants:
                            if not isinstance(participant, dict):
                                occupied_by.append("未知设备")
                                continue
                            alias = participant.get("alias")
                            participant_id = participant.get("device_id")
                            occupied_by.append(
                                alias
                                if isinstance(alias, str) and alias
                                else (
                                    participant_id
                                    if isinstance(participant_id, str) and participant_id
                                    else "未知设备"
                                )
                            )
                    devices.append(
                        {
                            "device_id": device_id,
                            "alias": str(item.get("alias") or ""),
                            "platform": item.get("platform"),
                            "online": item.get("status") == "CONNECTED",
                            "can_be_controlled": bool(
                                item.get("controllable") and item.get("controlled_support")
                            ),
                            "occupied": occupied,
                            "occupied_by": occupied_by,
                            "shared": shared,
                            "raw": item,
                        }
                    )
        self._device_list_cache = devices
        return devices

    async def validate_device_target(self, device_id: str) -> list[str]:
        """严格校验账号设备目标；仅列表请求失败时跳过未知性校验。"""

        normalized = str(device_id).strip()
        if not normalized:
            raise UuApiError("device_id 不能为空")
        if self.device_id and normalized == self.device_id:
            raise UuApiError("不能连接当前设备自身")
        devices = self._device_list_cache
        if devices is None:
            try:
                devices = await self.list_devices()
            except UuApiError:
                return []
        target = next(
            (device for device in devices if device["device_id"] == normalized),
            None,
        )
        if target is None:
            raise UuApiError(f"目标设备 {normalized} 不在账号设备列表中")
        if not target["can_be_controlled"]:
            raise UuApiError(f"目标设备 {normalized} 不可被控")
        if not target["online"]:
            raise UuApiError(f"目标设备 {normalized} 已离线")
        return list(target["occupied_by"])

    async def validate_assist_target(self, connect_id: str) -> list[str]:
        """尽力检查协助目标；connect_id 不在设备列表时允许继续。"""

        normalized = str(connect_id).strip()
        if not normalized:
            raise UuApiError("connect_id 不能为空")
        devices = self._device_list_cache
        if devices is None:
            try:
                devices = await self.list_devices()
            except UuApiError:
                return []
        target = next(
            (device for device in devices if device["device_id"] == normalized),
            None,
        )
        if target is None:
            return []
        if not target["can_be_controlled"]:
            raise UuApiError(f"目标设备 {normalized} 不可被控")
        if not target["online"]:
            raise UuApiError(f"目标设备 {normalized} 已离线")
        return list(target["occupied_by"])

    async def join_room_by_device(self, device_id: str, force_join: bool = False) -> dict[str, Any]:
        """进自有设备房间；返回上游原始响应（含 room_config）。"""
        self.require_account_identity()
        normalized_device_id = str(device_id).strip()
        if not normalized_device_id:
            raise UuApiError("device_id 不能为空")
        payload = await self._request(
            "POST",
            PATH_JOIN_BY_DEVICE % normalized_device_id,
            {"force_join": force_join},
            idempotent=False,
            operation="join-room-by-device",
        )
        _require_committed_room_config(payload, "join-room-by-device")
        return payload

    async def poll_assist(self, connect_id: str, *, raw: bool = False) -> dict[str, Any]:
        """Poll/join remote assistance as the account controller, never as a response."""
        self.require_account_identity()
        normalized_connect_id = str(connect_id).strip()
        if not normalized_connect_id:
            raise UuApiError("connect_id 不能为空")
        payload = await self._request(
            "POST",
            PATH_JOIN_BY_CONFIRMATION,
            {"connect_id": normalized_connect_id},
            idempotent=True,
            operation="poll-assist",
            raw=raw,
        )
        if payload.get("code") == 0:
            _require_committed_room_config(payload, "poll-assist")
        return payload

    async def get_room_control_mode(self, connect_id: str) -> str:
        """Return the validated remote-assistance authorization mode for a room."""
        self.require_account_identity()
        normalized_connect_id = _validate_guest_connect_id(connect_id)
        payload = await self._request(
            "POST",
            PATH_ROOM_CONTROL_MODE,
            {"connect_id": normalized_connect_id},
            idempotent=True,
            operation="room-control-mode",
        )
        control_mode = _dig(payload, "data", "control_mode")
        if control_mode is None:
            control_mode = _dig(payload, "control_mode")
        if (
            not _is_strict_identity_value(
                control_mode,
                max_length=_MAX_CONTROL_MODE_LENGTH,
            )
            or control_mode not in _ROOM_CONTROL_MODES
        ):
            raise UuApiError("对端设备未返回可识别的验证方式")
        return control_mode

    async def join_guest_by_code(self, connect_id: str, access_code: str) -> dict[str, Any]:
        """Join a password-protected guest room as an account controller."""
        self.require_account_identity()
        normalized_connect_id = _validate_guest_connect_id(connect_id)
        try:
            validated_code = validate_access_code(access_code)
        except ValueError as exc:
            raise UuApiError("connect_code 格式无效") from exc
        payload = await self._request(
            "POST",
            PATH_JOIN_BY_CODE,
            {"connect_id": normalized_connect_id, "connect_code": validated_code},
            idempotent=False,
            operation="join-guest-by-code",
            raw=False,
        )
        _require_committed_room_config(payload, "join-guest-by-code")
        return payload

_MAX_TOKEN_LENGTH = 16_384
_MAX_IDENTITY_FIELD_LENGTH = 512
_MAX_CONNECT_ID_LENGTH = 512
_MAX_CONTROL_MODE_LENGTH = 64
_MAX_RESPONSE_MSG_LENGTH = 512
_HTTP_ERROR_BODY_SNIPPET_LENGTH = 200
_MAX_RESPONSE_BYTES = 1 << 20
_ROOM_CONTROL_MODES = frozenset(
    {
        CONNECT_TYPE_BY_PASSWORD,
        CONNECT_TYPE_BY_CONFIRMATION,
        CONNECT_TYPE_PASSWORD_CONFIRMATION,
    }
)


def _normalize_device_name(name: str) -> str:
    """校验并规范化设备名称。"""
    if (
        type(name) is not str
        or any(unicodedata.category(character) == "Cc" for character in name)
    ):
        raise ValueError("设备名称必须为 1 到 32 个非控制字符")
    normalized = name.strip()
    if not 1 <= len(normalized) <= 32:
        raise ValueError("设备名称必须为 1 到 32 个非控制字符")
    return normalized


def _sanitize_response_msg(value: Any) -> str | None:
    """Return a bounded, single-line business message safe for exception reprs."""
    if type(value) is not str:
        return None
    sanitized = "".join(
        character if character.isprintable() else " "
        for character in value[:_MAX_RESPONSE_MSG_LENGTH]
    ).strip()
    return sanitized or None


def _outcome_error(*, idempotent: bool, operation: str) -> UuApiError:
    """Return a sanitized transport/outcome error for a static operation label."""
    if idempotent:
        return UuApiTransientError(f"UU API 可重试的暂时错误: {operation}")
    return UuApiUnknownOutcomeError(f"UU API 请求结果未知: {operation}")


def _malformed_success_error(*, idempotent: bool, operation: str, detail: str) -> UuApiError:
    """Classify a malformed 2xx response without exposing its contents."""
    if not idempotent:
        return _outcome_error(idempotent=False, operation=operation)
    return UuApiError(f"UU API {operation} {detail}")


def _committed_response_error(operation: str) -> UuApiCommittedResponseError:
    """Return a sanitized error for a committed response missing required data."""
    return UuApiCommittedResponseError(f"UU API 已提交响应不可用: {operation}")


def _required_committed_value(
    payload: dict[str, Any],
    operation: str,
    *,
    field_label: str,
    max_length: int,
) -> str:
    """Extract one strict, bounded code=0 data field without coercion."""
    value = _dig(payload, "data", field_label)
    if not _is_strict_identity_value(value, max_length=max_length):
        raise _committed_response_error(operation)
    return value


def _optional_committed_value(
    value: Any,
    operation: str,
    *,
    field_label: str,
    max_length: int,
) -> str | None:
    """Validate an optional identity claim before it can change local state."""
    if value is None:
        return None
    if not _is_strict_identity_value(value, max_length=max_length):
        raise _committed_response_error(operation)
    return value


def _required_local_identity_value(value: Any, *, field_label: str, max_length: int) -> str:
    """Reject an invalid locally generated device identity before sending it."""
    if not _is_strict_identity_value(value, max_length=max_length):
        raise UuApiError("设备注册配置身份无效")
    return value


def _is_strict_identity_value(value: Any, *, max_length: int) -> bool:
    """Accept only bounded, unpadded printable strings without control bytes."""
    return (
        type(value) is str
        and bool(value)
        and len(value) <= max_length
        and value == value.strip()
        and value.isprintable()
        and all(not (ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F) for character in value)
    )


def _require_committed_room_config(payload: dict[str, Any], operation: str) -> None:
    """Reject a code=0 response which cannot supply the room it committed."""
    if extract_room_config(payload) is None:
        raise _committed_response_error(operation)


def _dig(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _normalize_required_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise UuApiError(f"{field_name} 不能为空")
    normalized = value.strip()
    if not normalized:
        raise UuApiError(f"{field_name} 不能为空")
    return normalized


def _validate_guest_connect_id(value: Any) -> str:
    """Validate the numeric guest ID accepted by the password join endpoint."""
    if (
        not isinstance(value, str)
        or not 6 <= len(value) <= 12
        or any(character not in "0123456789" for character in value)
    ):
        raise UuApiError("connect_id 必须为 6 到 12 位数字")
    return value


def _validate_access_proof(proof: Any) -> str:
    if (
        not isinstance(proof, str)
        or len(proof) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in proof)
    ):
        raise UuApiError("sign 格式无效")
    return proof


def _jwt_field(token: str, field: str) -> Any:
    """解 JWT payload（不验签）取字段。"""
    import base64

    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return payload.get(field)
    except Exception:
        return None

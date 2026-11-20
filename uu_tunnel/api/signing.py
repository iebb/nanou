"""API 常量与请求签名。

签名基串：
    METHOD + path(含query) + 排序后的 "k=v&..." x-param 头拼接 + body 原文
签名：HMAC-SHA256(key, base) 的十六进制摘要，置于 X-Param-SIGN 头。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

API_BASE = "https://api.nrd.nie.163.com"

# 应用标识（对齐官方桌面端 4.23.0）
APP_PACKAGE = "com.netease.uuremote"
VERSION_CODE = "423000"
VERSION_NAME = "4.23.0"

# HMAC-SHA256 签名密钥（业务 API 主密钥；另一个 oK5z 开头的密钥是插件请求用的，别搞混）
APP_SIGNING_KEY = "alWiSzXZTLu3WfFnw13uBru3"

# 平台常量（X-Param-PLAT，官方桌面端为 "1"）
PLAT_WINDOWS = "1"

# 设备注册（桌面端）
PATH_DEVICE_INIT = "/api/v1/device/windows/init"

# 认证（短信登录）
PATH_MOBILE_CODE = "/api/v1/security/mobile/code"
PATH_MOBILE_LOGIN = "/api/v1/login/by_mobile"
PATH_GUEST_CREATE = "/api/v1/guest/create"

# 设备
PATH_DEVICE_LIST = "/api/v1/device/list"

# 房间（被控端创建 / 主控端加入）
PATH_ROOM_CREATE = "/api/v1/room/create"
PATH_GUEST_ROOM_CREATE = "/api/v1/guest/room/create"
PATH_GUEST_SHARE_INFO = "/api/v1/guest/share/info"
PATH_GUEST_SHARE_CONFIRMATION = "/api/v1/guest/room/share/confirmation"
PATH_GUEST_SHARE_UPLOAD_SIGN = "/api/v1/guest/room/share/upload/sign"
PATH_GUEST_UPLOAD_CONTROL_MODE = "/api/v1/guest/room/share/upload_control_mode"
PATH_JOIN_BY_DEVICE = "/api/v1/room/join/by_device/%s"
PATH_CLEAR_BY_DEVICE = "/api/v1/room/clear/by_device/%s"
PATH_JOIN_BY_CONFIRMATION = "/api/v2/room/join/share/by_confirmation"
PATH_JOIN_BY_CODE = "/api/v2/room/join/share/by_code"
PATH_ROOM_CONTROL_MODE = "/api/v2/room/share/control_mode"


class RequestSigner:
    """X-Param-* 公共头 + HMAC-SHA256 请求签名（桌面端身份）。"""

    def __init__(self, state: dict[str, Any]):
        self._state = state

    def common_headers(self) -> dict[str, str]:
        state = self._state
        return {
            "X-Param-CHN": str(state.get("channel") or "nochannel"),
            "X-Param-OPR": "",
            "X-Param-PKGN": APP_PACKAGE,
            "X-Param-PLAT": PLAT_WINDOWS,
            "X-Param-REL": "prod",
            "X-Param-VC": VERSION_CODE,
            "X-Param-VN": VERSION_NAME,
            "X-Param-ABI": "x86_64",
            "X-Param-client-id": str(state.get("client_id") or ""),
            "X-Param-device-id": str(state.get("device_id") or ""),
            "X-Param-system-id": str(state.get("system_id") or ""),
            "X-Param-user-id": str(state.get("user_id") or ""),
            "X-Param-OAID": "",
            "X-Param-TS": str(int(time.time())),
            "X-Param-CNT": "CN",
            "X-Param-LANG": "zh-CN",
        }

    def sign(self, method: str, path: str, headers: dict[str, str], body: str) -> str:
        pairs = sorted(
            (name.lower(), str(value))
            for name, value in headers.items()
            if name.lower().startswith("x-param-")
        )
        xparam = "&".join(f"{k}={v}" for k, v in pairs)
        base = f"{method.upper()}{path}{xparam}{body}"
        return hmac.new(APP_SIGNING_KEY.encode(), base.encode(), hashlib.sha256).hexdigest()

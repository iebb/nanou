"""登录态存取，复用统一的 uu_tunnel.json 配置。

每条账号记录以 device_id 为键，保存该设备身份的凭据与设备名；同一用户可以在
不同设备身份下各有一条记录，设备名随记录走。
"""

from __future__ import annotations

from typing import Any

from ..api import UuApiClient
from ..config import config_path, load, save


_CREDENTIAL_KEYS = (
    "client_id",
    "system_id",
    "device_id",
    "token",
    "user_id",
    "guest_id",
    "device_name",
)
_LEGACY_TOP_LEVEL_KEYS = ("device_name", "ice_policy")


def _accounts(data: dict[str, Any]) -> dict[str, Any]:
    accounts = data.get("accounts")
    return accounts if isinstance(accounts, dict) else {}


def load_client() -> UuApiClient:
    """从统一配置加载当前账号的凭据及设备名。"""
    data = load()
    active_account = data.get("active_account")
    accounts = _accounts(data)
    account = accounts.get(active_account) if isinstance(active_account, str) else None
    state = (
        {key: account[key] for key in _CREDENTIAL_KEYS if key in account}
        if isinstance(account, dict)
        else {}
    )
    legacy_name = data.get("device_name")
    if "device_name" not in state and isinstance(legacy_name, str):
        # 旧版本把设备名放在顶层；迁移到当前记录，下次保存时落盘。
        state["device_name"] = legacy_name
    return UuApiClient(state)


def save_client(client: UuApiClient) -> None:
    """保存客户端凭据与设备名到其 device_id 对应的记录，保留其他配置项。"""
    data = load()
    state = client.export_state()
    account_key = str(state.get("device_id") or "guest")
    accounts = _accounts(data)
    previous = accounts.get(account_key)
    account = {key: state[key] for key in _CREDENTIAL_KEYS if key in state}
    if isinstance(previous, dict) and "phone" in previous:
        account["phone"] = previous["phone"]
    accounts[account_key] = account
    data["accounts"] = accounts
    data["active_account"] = account_key
    for key in _LEGACY_TOP_LEVEL_KEYS:
        data.pop(key, None)
    save(data)


def clear_saved_auth() -> None:
    """删除当前账号，保留其他账号和设置。"""
    path = config_path()
    if path.is_file():
        data = load()
        accounts = _accounts(data)
        active_account = data.get("active_account")
        if isinstance(active_account, str):
            accounts.pop(active_account, None)
        data["accounts"] = accounts
        if accounts:
            data["active_account"] = next(iter(accounts))
        else:
            data.pop("active_account", None)
        save(data)


def list_accounts() -> list[dict]:
    """返回所有已保存账号（含设备身份与设备名）及当前账号标记。"""
    data = load()
    active_account = data.get("active_account")
    legacy_name = data.get("device_name")
    result: list[dict] = []
    for key, account in _accounts(data).items():
        if not isinstance(account, dict):
            continue
        name = account.get("device_name")
        if not name and key == active_account and isinstance(legacy_name, str):
            name = legacy_name
        result.append(
            {
                "key": str(key),
                "device_id": str(account.get("device_id") or key),
                "device_name": str(name or ""),
                "user_id": str(account.get("user_id") or ""),
                "phone": str(account.get("phone") or ""),
                "active": key == active_account,
            }
        )
    return result


def switch_account(key: str) -> None:
    """切换当前账号。"""
    data = load()
    accounts = _accounts(data)
    if key not in accounts:
        raise ValueError(f"账号不存在：{key}")
    data["active_account"] = key
    save(data)


def remove_account(key: str) -> None:
    """删除指定账号；删除当前账号时自动选择剩余账号。"""
    data = load()
    accounts = _accounts(data)
    if key not in accounts:
        return
    del accounts[key]
    data["accounts"] = accounts
    if data.get("active_account") == key:
        if accounts:
            data["active_account"] = next(iter(accounts))
        else:
            data.pop("active_account", None)
    save(data)


def set_account_phone(key: str, phone: str) -> None:
    """记录指定账号登录时使用的手机号。"""
    data = load()
    accounts = _accounts(data)
    account = accounts.get(key)
    if not isinstance(account, dict):
        raise ValueError(f"账号不存在：{key}")
    account["phone"] = phone
    data["accounts"] = accounts
    save(data)

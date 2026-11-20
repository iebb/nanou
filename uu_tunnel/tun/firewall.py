"""Windows 防火墙规则。

入站允许规则按隧道源 CIDR 限定，而不按接口名称限定，因为 Wintun 友好名称
无法可靠地通过 COM API 绑定到防火墙规则。
"""

from __future__ import annotations

import asyncio
import logging
import os
from ipaddress import IPv4Network
from typing import Any, Final


logger = logging.getLogger(__name__)

_RULE_PREFIX: Final[str] = "uutunnel-in-"
_NET_FW_RULE_DIRECTION_IN: Final[int] = 1
_NET_FW_ACTION_ALLOW: Final[int] = 1
_NET_FW_PROFILE2_ALL: Final[int] = 0x7FFFFFFF
_NET_FW_IP_PROTOCOL_ANY: Final[int] = 256


def _rule_name(if_index: int) -> str:
    return f"{_RULE_PREFIX}{if_index}"


def _rule_exists(rules: Any, rule_name: str) -> bool:
    return any(rule.Name == rule_name for rule in rules)


def _allow_tunnel_adapter_blocking(
    if_index: int,
    adapter_name: str,
    network: IPv4Network,
) -> None:
    """添加按源 CIDR 限定的入站规则。

    Wintun 友好名称无法可靠地通过 COM API 绑定；adapter_name 保留给调用方兼容性及日志。
    """
    try:
        import pythoncom
        import win32com.client
    except Exception:
        logger.warning("无法导入 Windows 防火墙 COM API", exc_info=True)
        return

    try:
        pythoncom.CoInitialize()
    except pythoncom.com_error:
        logger.warning("初始化 Windows 防火墙 COM API 失败", exc_info=True)
        return

    try:
        policy = win32com.client.Dispatch("HNetCfg.FwPolicy2")
        rules = policy.Rules
        rule_name = _rule_name(if_index)
        if _rule_exists(rules, rule_name):
            return

        logger.debug(
            "创建隧道入站防火墙规则：名称=%s，适配器=%s，源 CIDR=%s",
            rule_name,
            adapter_name,
            network,
        )
        rule = win32com.client.Dispatch("HNetCfg.FWRule")
        rule.Name = rule_name
        rule.Description = "uu-tunnel inbound allow for tunnel adapter"
        rule.Direction = _NET_FW_RULE_DIRECTION_IN
        rule.Action = _NET_FW_ACTION_ALLOW
        rule.Profiles = _NET_FW_PROFILE2_ALL
        rule.RemoteAddresses = str(network)
        rule.Protocol = _NET_FW_IP_PROTOCOL_ANY
        rule.Enabled = True
        rules.Add(rule)
    except pythoncom.com_error:
        logger.warning("配置隧道适配器防火墙规则失败", exc_info=True)
    except Exception:
        logger.warning("配置隧道适配器防火墙规则失败", exc_info=True)
    finally:
        rule = None
        rules = None
        policy = None
        pythoncom.CoUninitialize()


def _remove_tunnel_adapter_rules_blocking(if_index: int) -> None:
    try:
        import pythoncom
        import win32com.client
    except Exception:
        logger.warning("无法导入 Windows 防火墙 COM API", exc_info=True)
        return

    try:
        pythoncom.CoInitialize()
    except pythoncom.com_error:
        logger.warning("初始化 Windows 防火墙 COM API 失败", exc_info=True)
        return

    try:
        policy = win32com.client.Dispatch("HNetCfg.FwPolicy2")
        rules = policy.Rules
        rule_name = _rule_name(if_index)
        if _rule_exists(rules, rule_name):
            rules.Remove(rule_name)
    except pythoncom.com_error:
        logger.warning("移除隧道适配器防火墙规则失败", exc_info=True)
    except Exception:
        logger.warning("移除隧道适配器防火墙规则失败", exc_info=True)
    finally:
        rules = None
        policy = None
        pythoncom.CoUninitialize()


async def allow_tunnel_adapter(
    if_index: int,
    adapter_name: str,
    network: IPv4Network,
) -> None:
    """允许来自隧道源 CIDR 的入站流量。

    规则适用于任意接口但仅限隧道源 CIDR，因为 Wintun 友好名称无法可靠地通过
    COM API 绑定到防火墙规则。
    """

    if os.name != "nt":
        return
    try:
        await asyncio.to_thread(
            _allow_tunnel_adapter_blocking,
            if_index,
            adapter_name,
            network,
        )
    except Exception:
        logger.warning("配置隧道适配器防火墙规则失败", exc_info=True)


async def remove_tunnel_adapter_rules(if_index: int) -> None:
    """移除一个 Windows 隧道适配器的防火墙规则。"""

    if os.name != "nt":
        return
    try:
        await asyncio.to_thread(_remove_tunnel_adapter_rules_blocking, if_index)
    except Exception:
        logger.warning("移除隧道适配器防火墙规则失败", exc_info=True)


__all__ = ["allow_tunnel_adapter", "remove_tunnel_adapter_rules"]

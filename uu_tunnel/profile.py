"""设备注册指纹。"""

from __future__ import annotations

import hashlib
import random
import uuid
from typing import Any


_ARCHETYPES = (
    {
        "form_factor": "desktop",
        "cpu": "13th Gen Intel(R) Core(TM) i5-13400F",
        "video": ("NVIDIA GeForce RTX 4060",),
        "memory": 16384,
        "screen": "1920x1080",
        "base_board": "ASUSTeK COMPUTER INC. PRIME B760M-A D4",
        "video_driver": "32.0.15.6094",
    },
    {
        "form_factor": "desktop",
        "cpu": "AMD Ryzen 5 5600 6-Core Processor",
        "video": ("NVIDIA GeForce RTX 3060",),
        "memory": 16384,
        "screen": "1920x1080",
        "base_board": "Micro-Star International Co., Ltd. MAG B550 TOMAHAWK (MS-7C91)",
        "video_driver": "31.0.15.5123",
    },
    {
        "form_factor": "desktop",
        "cpu": "AMD Ryzen 7 7800X3D 8-Core Processor",
        "video": ("NVIDIA GeForce RTX 4070 SUPER",),
        "memory": 32768,
        "screen": "2560x1440",
        "base_board": "Gigabyte Technology Co., Ltd. B650M AORUS ELITE AX",
        "video_driver": "32.0.15.6109",
    },
    {
        "form_factor": "laptop",
        "cpu": "13th Gen Intel(R) Core(TM) i7-13700HX",
        "video": ("NVIDIA GeForce RTX 4060 Laptop GPU", "Intel(R) UHD Graphics"),
        "memory": 16384,
        "screen": "2560x1600",
        "base_board": "LENOVO LNVNB161216",
        "video_driver": "32.0.15.6094",
    },
    {
        "form_factor": "desktop",
        "cpu": "12th Gen Intel(R) Core(TM) i5-12400",
        "video": ("Intel(R) UHD Graphics 730",),
        "memory": 16384,
        "screen": "1920x1080",
        "base_board": "ASUSTeK COMPUTER INC. PRIME H610M-K D4",
        "video_driver": "31.0.101.5590",
    },
    {
        "form_factor": "laptop",
        "cpu": "AMD Ryzen 7 7840HS w/ Radeon 780M Graphics",
        "video": ("AMD Radeon(TM) 780M Graphics",),
        "memory": 32768,
        "screen": "2880x1800",
        "base_board": "ASUSTeK COMPUTER INC. ROG Zephyrus G14 GA403UI_GA403UI",
        "video_driver": "31.0.24033.1003",
    },
)

_MAC_OUIS = (
    (0x00, 0xE0, 0x4C),
    (0x2C, 0xF0, 0x5D),
    (0xB4, 0x2E, 0x99),
    (0x04, 0xD4, 0xC4),
    (0x04, 0xD9, 0xF5),
    (0x2C, 0x4D, 0x54),
)
_OS_VERSIONS = (
    "Windows 10 Pro (10.0.19045)",
    "Windows 11 Pro (10.0.22631)",
    "Windows 11 Home (10.0.22631)",
)
_HOSTNAME_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def stable_machine_identity() -> str:
    """返回免登录可得、与设备注册 profile 同源的稳定本机身份。"""

    seed = f"uu-tunnel-machine-{uuid.getnode():012x}"
    return str(generate_device_profile(seed)["machine_guid"])


def generate_device_profile(
    client_id: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """生成稳定的 Windows 设备注册指纹。"""

    if client_id is None:
        client_id = uuid.uuid4().hex
    fingerprint = hashlib.sha256(client_id.encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(fingerprint[:8], "big"))
    archetype = rng.choice(_ARCHETYPES)
    system_id = uuid.UUID(int=rng.getrandbits(128), version=4).hex
    machine_guid = str(uuid.UUID(bytes=fingerprint[:16]))
    mac_bytes = (*rng.choice(_MAC_OUIS), *(rng.randrange(256) for _ in range(3)))
    mac = "-".join(f"{byte:02X}" for byte in mac_bytes)
    prefix = "LAPTOP" if archetype["form_factor"] == "laptop" else "DESKTOP"
    hostname = f"{prefix}-{''.join(rng.choice(_HOSTNAME_CHARS) for _ in range(7))}"
    custom_name = name.strip() if isinstance(name, str) else ""

    return {
        "name": custom_name or hostname,
        "client_id": client_id,
        "system_id": system_id,
        "machine_guid": machine_guid,
        "os": rng.choice(_OS_VERSIONS),
        "base_board": archetype["base_board"],
        "cpu": archetype["cpu"],
        "video": list(archetype["video"]),
        "mac": mac,
        "memory": archetype["memory"],
        "screen": archetype["screen"],
        "platform": 1,
        "controllable": True,
        "video_driver": archetype["video_driver"],
        "subnet_mask": "255.255.255.0",
    }

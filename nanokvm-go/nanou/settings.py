"""Parse SDK settings for the touchscreen and the background service."""

import unicodedata
from dataclasses import dataclass, field


def enabled_setting(values, key, label):
    value = str(values.get(key, "false")).strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"{label} must be true or false")


@dataclass(frozen=True)
class Settings:
    username: str
    password: str = field(repr=False)
    tls_pin: str
    name: str
    autostart: bool
    audio: bool
    mcp_key: str = field(repr=False)
    sensitivity: int

    @classmethod
    def from_env(cls, values):
        name = values.get("NANOU_UU_NAME", "").strip()
        if len(name) > 32 or any(unicodedata.category(c).startswith("C") for c in name):
            raise ValueError("UU name must be 32 visible characters or fewer")
        try:
            sensitivity = int(values.get("NANOU_MOUSE_SENSITIVITY", "100"))
        except ValueError:
            sensitivity = 0
        if not 25 <= sensitivity <= 400:
            raise ValueError("Mouse sensitivity must be 25 to 400 percent")
        username = values.get("NANOU_KVM_USER", "").strip()
        password = values.get("NANOU_KVM_PASSWORD", "")
        if not username or not password:
            raise ValueError("Set the local login in Apps settings")
        return cls(
            username=username,
            password=password,
            tls_pin=values.get("NANOU_KVM_TLS_PIN", ""),
            name=name,
            autostart=enabled_setting(values, "NANOU_AUTOSTART", "Boot startup"),
            audio=enabled_setting(values, "NANOU_AUDIO", "Audio"),
            mcp_key=values.get("NANOU_KVM_MCP_KEY", "").strip(),
            sensitivity=sensitivity,
        )

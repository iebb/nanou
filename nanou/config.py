"""Shared NanoKVM address and local state settings."""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

WEB_PORTS = {"http": 80, "https": 443}


def state_directory():
    return Path(os.environ.get("NANOU_STATE_DIR", Path.home() / ".nanou")).expanduser().resolve()


@dataclass(frozen=True)
class Destination:
    scheme: str
    host: str
    port: int

    @classmethod
    def from_url(cls, url):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in WEB_PORTS
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or "?" in url
            or "#" in url
            or parsed.port == 0
            or any(character.isspace() or ord(character) < 32 for character in url)
        ):
            raise ValueError("Enter a NanoKVM base URL such as http://192.168.1.42.")
        return cls(parsed.scheme, parsed.hostname, parsed.port or WEB_PORTS[parsed.scheme])

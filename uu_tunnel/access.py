"""Official UU remote-assistance access-code primitives.

These helpers intentionally keep code generation, local validation, and the
host proof independent from room/workflow policy.
"""

from __future__ import annotations

import hashlib
import secrets

ACCESS_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ123456789"
ACCESS_CODE_LENGTH = 8
ACCESS_CODE_MIN_LENGTH = 3
ACCESS_CODE_MAX_LENGTH = 64

CONNECT_TYPE_BY_PASSWORD = "by_password"
CONNECT_TYPE_BY_CONFIRMATION = "by_confirmation"
CONNECT_TYPE_PASSWORD_CONFIRMATION = "password_confirmation"


def generate_access_code() -> str:
    """Generate the product's eight-character remote-assistance code."""
    return "".join(secrets.choice(ACCESS_CODE_ALPHABET) for _ in range(ACCESS_CODE_LENGTH))


def validate_access_code(value: str) -> str:
    """Validate and return an unchanged user-supplied access code.

    The desktop product accepts the official alphabet only. Whitespace and
    lowercase characters are rejected rather than normalized, so a displayed
    code and the proof input always refer to the same bytes.
    """
    if not isinstance(value, str):
        raise ValueError("access code must be a string")
    if value != value.strip():
        raise ValueError("access code must not have surrounding whitespace")
    if not ACCESS_CODE_MIN_LENGTH <= len(value) <= ACCESS_CODE_MAX_LENGTH:
        raise ValueError("access code length is invalid")
    if any(character not in ACCESS_CODE_ALPHABET for character in value):
        raise ValueError("access code has invalid characters")
    return value


def validate_product_access_code(value: str) -> str:
    """Validate the fixed eight-character code used by product workflows."""
    code = validate_access_code(value)
    if len(code) != ACCESS_CODE_LENGTH:
        raise ValueError("product access code must be exactly eight characters")
    return code


def make_access_proof(salt: str, code: str) -> str:
    """Return the official SHA-256 proof for ``salt + code``."""
    if not isinstance(salt, str) or not salt:
        raise ValueError("salt must be a non-empty string")
    code = validate_access_code(code)
    return hashlib.sha256((salt + code).encode("utf-8")).hexdigest()

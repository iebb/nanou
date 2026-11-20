"""UU 开放 API 层：常量、签名、HTTP 传输。


"""

from .client import (
    DeviceRecord,
    UuApiClient,
    UuApiError,
    UuApiResponseError,
    UuApiTransientError,
    UuApiUnknownOutcomeError,
    UuApiCommittedResponseError,
    UuAuthExpiredError,
)
from .room import RoomConfig, extract_room_config

__all__ = [
    "DeviceRecord",
    "UuApiClient",
    "UuApiError",
    "UuApiResponseError",
    "UuApiTransientError",
    "UuApiUnknownOutcomeError",
    "UuApiCommittedResponseError",
    "UuAuthExpiredError",
    "RoomConfig",
    "extract_room_config",
]

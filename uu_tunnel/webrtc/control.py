"""Official-shaped UU controller ``control`` event payloads."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from . import connect_options_pb2


_MAX_DEVICE_ID_LENGTH = 128
_CLIENT_WINDOWS = 3
_CONTROL_CONNECT_NORMAL = 1
_CONTROL_CONNECT_ASSISTANCE = 2


@dataclass(frozen=True, slots=True)
class ControlPayload:
    """Binary and JSON data carried by one controller ``control`` event."""

    app_control_id: str = field(repr=False)
    app_data: bytes = field(repr=False)
    streamer_data: str = field(repr=False)

    def socket_payload(self) -> dict[str, str | bytes]:
        """Return the ordered Socket.IO event body."""
        return {
            "app_control_id": self.app_control_id,
            "app_data": self.app_data,
            "streamer_data": self.streamer_data,
        }


def build_control_payload(device_id: str, assistance: bool) -> ControlPayload:
    """Build the official controller payload for a target device."""
    _validate_device_id(device_id)
    if type(assistance) is not bool:
        raise TypeError("assistance must be a bool")

    control_id = str(uuid.uuid4()).lower()
    return ControlPayload(
        app_control_id=control_id,
        app_data=_build_app_data(device_id, assistance),
        streamer_data=_build_streamer_data(control_id),
    )


def _validate_device_id(device_id: str) -> None:
    if not isinstance(device_id, str):
        raise TypeError("device_id must be a string")
    if not device_id or device_id != device_id.strip():
        raise ValueError("device_id must be a nonempty, unpadded string")
    if len(device_id) > _MAX_DEVICE_ID_LENGTH:
        raise ValueError("device_id exceeds the maximum length")


def _build_app_data(
    device_id: str,
    assistance: bool,
    *,
    client_type: int = _CLIENT_WINDOWS,
    cursor_capture: bool = False,
) -> bytes:
    """Build deterministic ConnectOptions bytes; overrides are validation-only."""
    options = connect_options_pb2.ConnectOptions()
    options.capture_type = 1  # CT_DESKTOP
    options.type_value = -1

    capture_params = options.capture_params
    capture_params.fps = 2  # FPS_60
    capture_params.video_quality = 3  # VideoQuality_HD
    capture_params.cursor_capture = cursor_capture
    capture_params.choose_resolution_type = 1  # ChooseType_DEFAULT
    capture_params.local_resolution.width = 1920
    capture_params.local_resolution.height = 1080

    decoder_cap = options.decoder_cap_list.add()
    decoder_cap.fps = 60
    decoder_cap.codec_type = 1  # CodecType_H264
    decoder_cap.resolution_width = 3840
    decoder_cap.resolution_height = 2160
    decoder_cap.chroma_format = 1  # ChromaFormat_420

    virtual_display_mode = options.virtual_display_modes.add()
    virtual_display_mode.width = 1920
    virtual_display_mode.height = 1080
    virtual_display_mode.fps = 60

    options.client_type = client_type
    options.device_id = device_id
    options.control_connect_type = (
        _CONTROL_CONNECT_ASSISTANCE if assistance else _CONTROL_CONNECT_NORMAL
    )

    feature_flag = options.feature_flag
    feature_flag.ff_capture_setting = 2
    feature_flag.ff_simple_action = 1
    feature_flag.ff_system_metrics = 2
    feature_flag.ff_private_screen = 2
    feature_flag.ff_update_acquire = 0
    feature_flag.ff_file_transfer_ftp = 2
    feature_flag.ff_file_transfer_ftp2 = 2
    feature_flag.ff_clipboard = 3
    feature_flag.ff_qos_stat = 0
    feature_flag.ff_mumu_control = 0
    feature_flag.ff_virtual_mouse_device = 0

    options.client_version = "4.23.0"
    return options.SerializeToString(deterministic=True)


def _build_streamer_data(control_id: str) -> str:
    return json.dumps(
        {
            "control_id": control_id,
            "device_capability": {
                "display_info": [{"id": 0, "fps": 60, "type": 0, "hdr": -1}],
                "video_codec_capability": [
                    {
                        "video_codec": 1,
                        "width": 3840,
                        "height": 2160,
                        "chroma_sampling": 1,
                        "bit_depth": 8,
                        "codec_impl": -1,
                    }
                ],
                "ice_id": "",
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

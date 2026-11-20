"""Regression checks for the NanoKVM Go app configuration."""

import json
import stat
import struct
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).with_name("nanou")))

import boot
from audio import SpeakerSession
from bridge import FirmwareError, HID
from settings import Settings

ENV = {"NANOU_KVM_USER": "test-local-user", "NANOU_KVM_PASSWORD": "fixture"}


class OptionTests(unittest.TestCase):
    def test_defaults_preserve_foreground_and_pointer_behavior(self):
        settings = Settings.from_env(ENV)
        self.assertFalse(settings.autostart)
        self.assertFalse(settings.audio)
        self.assertEqual(settings.sensitivity, 100)
        self.assertEqual(settings.name, "")

    def test_options_are_read_and_validated(self):
        settings = Settings.from_env(ENV | {
            "NANOU_UU_NAME": "  Office KVM ",
            "NANOU_AUTOSTART": "true",
            "NANOU_AUDIO": "on",
            "NANOU_KVM_MCP_KEY": "test-key",
            "NANOU_MOUSE_SENSITIVITY": "175",
        })
        self.assertEqual((settings.name, settings.autostart, settings.audio, settings.sensitivity),
                         ("Office KVM", True, True, 175))
        with self.assertRaises(ValueError):
            Settings.from_env(ENV | {"NANOU_MOUSE_SENSITIVITY": "401"})

    def test_sensitivity_keeps_fractional_motion_and_absolute_coordinates(self):
        peer = Mock()
        hid = HID(peer, sensitivity=50)
        relative = {"event": "relative", "x": 1, "y": -1}
        hid.event(relative, None)
        peer.send_binary.assert_not_called()
        hid.event(relative, None)
        self.assertEqual(struct.unpack("<Bhhh", peer.send_binary.call_args.args[0][13:]), (0, 1, -1, 0))
        hid.event({"event": "absolute", "x": 0.25, "y": 0.75}, None)
        self.assertEqual(struct.unpack("<BHHh", peer.send_binary.call_args.args[0][13:]), (0, 16384, 49151, 0))

    def test_boot_settings_are_private_and_restart_on_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = path / "settings.json"
            unit = path / boot.UNIT
            settings = replace(Settings.from_env(ENV), autostart=True)
            with patch.multiple(boot, SERVICE_DIR=path, CONFIG=config, UNIT_PATH=unit), \
                    patch.object(boot.os, "geteuid", return_value=0), \
                    patch.object(Path, "is_dir", return_value=True), patch.object(boot, "systemd") as systemd:
                boot.enable(settings)
                self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
                self.assertTrue(json.loads(config.read_text())["autostart"])
                self.assertIn("--service", unit.read_text())
                systemd.assert_any_call("start", boot.UNIT)
                boot.enable(replace(settings, sensitivity=200))
                systemd.assert_any_call("restart", boot.UNIT)
                boot.disable()
                systemd.assert_any_call("disable", "--now", boot.UNIT)
                self.assertFalse(config.exists())
                self.assertFalse(unit.exists())

    def test_audio_requests_target_speaker_only_and_closes_the_session(self):
        session = SpeakerSession(Mock(), "test-key")
        spec = {"token": "fixture", "signalingPath": "/api/media/audio/webrtc?media_session=fixture"}
        with patch.object(session, "request", side_effect=[{}, None, {"structuredContent": spec}, {}]) as request:
            with session as media:
                self.assertEqual(media.path, spec["signalingPath"])
            create = request.call_args_list[2].args[1]
            self.assertIs(create["arguments"]["speakerReceive"], True)
            self.assertIs(create["arguments"]["micSend"], False)
            self.assertEqual(request.call_args_list[3].args[1]["name"], "media_session_close")

    def test_audio_rejects_nonlocal_signaling(self):
        session = SpeakerSession(Mock(), "test-key")
        spec = {"token": "fixture", "signalingPath": "wss://other.invalid/api/media/audio/webrtc"}
        with patch.object(session, "request", side_effect=[{}, None, {"structuredContent": spec}, {}]) as request:
            with self.assertRaises(FirmwareError):
                session.__enter__()
            self.assertEqual(request.call_args_list[3].args[1]["name"], "media_session_close")


if __name__ == "__main__":
    unittest.main()

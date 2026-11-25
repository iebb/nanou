"""Exercise the Linux bridge against an isolated HTTPS KVM fixture."""

import asyncio
import hashlib
import json
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from aiohttp import web
from websocket import WebSocketTimeoutException

sys.path.insert(0, str(Path(__file__).with_name("nanou")))
sys.path.insert(0, str(Path(__file__).with_name("host")))

from bridge import Bridge, Firmware, FirmwareError, HID
from bridge_cli import target
from runtime import HostRuntime, run_host
from settings import Settings

FRAME = bytes([1]) + struct.pack("<Q", 12345) + bytes.fromhex("000000016588")
HOST_PROBE = """#!/usr/bin/env python3
import json
import os
import socket
import struct
import time
from pathlib import Path

directory = Path(os.environ["NANOU_DEVICE_SOCKET_DIR"])
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as video:
    video.connect(str(directory / "video"))
    with video.makefile("rb") as wire:
        size, = struct.unpack(">I", wire.read(4))
        packet = wire.read(size)
    Path(os.environ["XDG_CONFIG_HOME"], "frame.hex").write_text(packet.hex())
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as control:
        for event in (
            {"event": "key", "key": 65, "down": True},
            {"event": "relative", "x": 3, "y": -2},
        ):
            control.sendto(json.dumps(event).encode(), str(directory / "input"))
    time.sleep(30)
"""


class LinuxBridgeTests(unittest.IsolatedAsyncioTestCase):
    def test_video_reports_firmware_no_image_status(self):
        upstream = Mock()
        upstream.recv.side_effect = [
            '{"event":"video-status","data":"-1"}', WebSocketTimeoutException(),
        ]
        receiver = Mock()
        receiver.accept.side_effect = [(MagicMock(), None), OSError()]
        bridge = SimpleNamespace(
            video=receiver, stopping=threading.Event(), firmware=Mock(), status=Mock(),
            iphone_drag_repair=False, wake_armed=threading.Event(),
            wake_requested=threading.Event(),
        )
        bridge.wake_armed.set()
        bridge.firmware.ws.return_value = upstream
        Bridge.video_loop(bridge)
        bridge.status.assert_called_once_with("No video signal from the attached computer")
        self.assertTrue(bridge.wake_requested.is_set())
        self.assertFalse(bridge.wake_armed.is_set())

    def test_input_reopens_after_failed_heartbeat(self):
        old_peer = Mock()
        old_peer.send_binary.side_effect = [None, BrokenPipeError()]
        new_peer = Mock()
        firmware = Mock()
        firmware.ws.side_effect = [old_peer, new_peer]
        down = b'{"event":"key","key":65,"down":true}'
        receiver = Mock()
        clock = Mock()
        clock.monotonic.return_value = 0

        def reports():
            yield down
            clock.monotonic.return_value = 11
            yield socket.timeout()
            yield down
            yield OSError()

        receiver.recv.side_effect = reports()
        bridge = SimpleNamespace(
            input=receiver, stopping=threading.Event(), firmware=firmware,
            status=Mock(), sensitivity=100, iphone_drag_repair=False, click_to_touch=False,
            iphone_video_recovered=threading.Event(), wake_requested=threading.Event(),
        )
        with patch("bridge.time", clock):
            Bridge.input_loop(bridge)
        self.assertEqual(firmware.ws.call_count, 2)
        old_peer.close.assert_called_once()
        recovered_report = new_peer.send_binary.call_args_list[0].args[0]
        self.assertEqual(recovered_report[13:], b"\x00\x00\x04\x00\x00\x00\x00\x00")

    def test_iphone_repair_precedes_pointer_after_reconnect(self):
        first, second = Mock(), Mock()
        pointer = b'{"event":"absolute","x":0.25,"y":0.25}'
        receiver = Mock()
        video_recovered = threading.Event()

        def reports():
            yield pointer
            yield pointer
            yield b'{"event":"reset"}'
            yield pointer
            video_recovered.set()
            yield pointer
            yield OSError()

        receiver.recv.side_effect = reports()
        bridge = SimpleNamespace(
            input=receiver, stopping=threading.Event(), firmware=Mock(),
            status=Mock(), sensitivity=100, iphone_drag_repair=True, click_to_touch=False,
            iphone_video_recovered=video_recovered, wake_requested=threading.Event(), wake_on_no_signal=False,
        )
        bridge.firmware.ws.side_effect = [first, second]
        with patch("bridge.time.sleep") as delay:
            Bridge.input_loop(bridge)

        repair = [(17, (0, 32768, 32768, 1)), (17, (0, 32768, 32768, -1))]
        move = [(2, (0, 16384, 16384, 0))]
        for peer, expected in ((first, repair + move + move), (second, (repair + move) * 2)):
            packets = [entry.args[0] for entry in peer.send_binary.call_args_list]
            decoded = [(packet[2], struct.unpack("<BHHh", packet[13:])) for packet in packets[:len(expected)]]
            self.assertTrue(all(packet[3] == 3 for packet in packets[:len(expected)]))
            self.assertEqual(decoded, expected)
        self.assertEqual([call.args for call in delay.call_args_list], [(0.030,)] * 3)

    def test_wake_sends_one_relative_movement_without_buttons(self):
        receiver = Mock()
        receiver.recv.side_effect = [socket.timeout(), socket.timeout(), OSError()]
        peer = Mock()
        requested = threading.Event()
        requested.set()
        bridge = SimpleNamespace(
            input=receiver, stopping=threading.Event(), firmware=Mock(),
            status=Mock(), sensitivity=200, iphone_drag_repair=False, click_to_touch=False,
            wake_requested=requested,
        )
        bridge.firmware.ws.return_value = peer
        Bridge.input_loop(bridge)
        movements = [call.args[0] for call in peer.send_binary.call_args_list if call.args[0][2:4] == b"\x02\x02"]
        self.assertEqual([struct.unpack("<Bhhh", packet[13:]) for packet in movements], [(0, 1, 0, 0), (0, 0, 0, 0)])

    def test_touchscreen_tap_drag_and_reset(self):
        peer = Mock()
        hid = HID(peer, click_to_touch=True)
        click = {"event": "button_click", "button": 1}
        start = {"event": "absolute", "x": 0.25, "y": 0.75}
        moved = {"event": "absolute", "x": 0.5, "y": 0.5}
        down = {"event": "button", "button": 1, "down": True}

        hid.event(click, None)
        hid.event(start, None)
        peer.send_binary.assert_not_called()
        with patch("bridge.time.sleep") as delay:
            hid.event(click, None)
        delay.assert_called_once()
        hid.event(down, None)
        hid.event(moved, None)
        hid.reset()

        packets = [entry.args[0] for entry in peer.send_binary.call_args_list]
        self.assertEqual([packet[2:4] for packet in packets], [b"\x13\x05"] * 7 + [b"\x01\x01"])
        reports = [struct.unpack("<BBBHHBB", packet[13:]) if len(packet) > 14 else None for packet in packets[:-1]]
        self.assertEqual(reports, [
            (1, 1, 3, 16384, 49151, 255, 0),
            (1, 1, 2, 16384, 49151, 0, 0),
            None,
            (1, 1, 3, 16384, 49151, 255, 0),
            (1, 1, 3, 32768, 32768, 255, 0),
            (1, 1, 2, 32768, 32768, 0, 0),
            None,
        ])

    def test_https_urls_and_port(self):
        self.assertEqual(target("https://kvm.example:9443/"), ("kvm.example", 9443))
        self.assertEqual(target("https://[::1]/"), ("::1", 443))
        with self.assertRaises(ValueError):
            target("http://kvm.example")

    async def test_pinned_https_routes_video_and_control_over_unix_sockets(self):
        with tempfile.TemporaryDirectory(prefix="nanou-test-", dir="/tmp") as temp:
            folder = Path(temp)
            cert = folder / "cert.pem"
            key = folder / "key.pem"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                 "-subj", "/CN=nanou-test", "-keyout", str(key), "-out", str(cert)],
                check=True, capture_output=True,
            )
            pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            received = asyncio.Queue()
            logins = []

            async def login(request):
                logins.append(await request.json())
                self.assertEqual(request.headers["Origin"], f"https://{request.host}")
                result = web.json_response({"code": 0})
                self.assertNotIn("fixture_session", request.cookies)
                result.set_cookie("fixture_session", "expired" if len(logins) == 1 else "valid")
                return result

            async def endpoint(request):
                if request.cookies.get("fixture_session") == "expired":
                    raise web.HTTPUnauthorized()
                self.assertEqual(request.cookies.get("fixture_session"), "valid")
                self.assertEqual(request.headers["Origin"], f"https://{request.host}")
                stream = web.WebSocketResponse()
                await stream.prepare(request)
                if request.path.endswith("direct"):
                    await stream.send_bytes(FRAME)
                async for incoming in stream:
                    if incoming.type == web.WSMsgType.BINARY:
                        await received.put(incoming.data)
                return stream

            kvm = web.Application()
            kvm.router.add_post("/api/auth/login", login)
            kvm.router.add_get("/api/stream/h264/direct", endpoint)
            kvm.router.add_get("/api/ws", endpoint)
            runner = web.AppRunner(kvm)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context).start()
            port = runner.addresses[0][1]
            try:
                wrong_pin = Firmware("0" * 64, "127.0.0.1", port)
                with self.assertRaises(FirmwareError):
                    await asyncio.to_thread(wrong_pin.login, "fixture", "fixture")
                self.assertEqual(logins, [])

                bundle = folder / "bundle"
                (bundle / "bin").mkdir(parents=True)
                (bundle / "lib").mkdir()
                (bundle / "bin" / "uuremote-server").touch()
                loader = bundle / "lib" / "fixture-runner"
                loader.write_text(HOST_PROBE)
                loader.chmod(0o700)
                state = folder / "linux-state"
                state.mkdir()
                config = state / "native-uu.json"
                config.write_text(json.dumps({"token": "fixture", "device_id": "fixture"}))
                sockets = folder / "sockets"
                sockets.mkdir()
                runtime = HostRuntime(bundle, state, config, state, loader.name)
                settings = Settings.from_env({
                    "NANOU_KVM_USER": "fixture-user",
                    "NANOU_KVM_PASSWORD": "fixture-password",
                    "NANOU_KVM_TLS_PIN": pin,
                    "NANOU_MOUSE_SENSITIVITY": "200",
                })
                cancel = threading.Event()
                status = []
                work = asyncio.create_task(asyncio.to_thread(
                    run_host, settings, sockets, cancel, status.append,
                    runtime=runtime, address=("127.0.0.1", port),
                ))
                try:
                    keyboard = await asyncio.wait_for(received.get(), 8)
                    mouse = await asyncio.wait_for(received.get(), 8)
                    self.assertEqual(keyboard[13:], b"\x00\x00\x04\x00\x00\x00\x00\x00")
                    self.assertEqual(struct.unpack("<Bhhh", mouse[13:]), (0, 6, -4, 0))
                    self.assertEqual((state / "frame.hex").read_text(), FRAME.hex())
                    self.assertEqual(logins, [{"username": "fixture-user", "password": "fixture-password"}] * 2)
                finally:
                    cancel.set()
                    self.assertTrue(await asyncio.wait_for(work, 12), status)
            finally:
                await runner.cleanup()


if __name__ == "__main__":
    unittest.main()

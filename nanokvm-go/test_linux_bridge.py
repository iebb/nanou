"""Exercise the Linux bridge against an isolated HTTPS KVM fixture."""

import asyncio
import hashlib
import json
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).with_name("nanou")))
sys.path.insert(0, str(Path(__file__).with_name("host")))

from bridge import Firmware, FirmwareError
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
                result.set_cookie("fixture_session", "valid")
                return result

            async def endpoint(request):
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
                    self.assertEqual(logins, [{"username": "fixture-user", "password": "fixture-password"}])
                finally:
                    cancel.set()
                    self.assertTrue(await asyncio.wait_for(work, 12), status)
            finally:
                await runner.cleanup()


if __name__ == "__main__":
    unittest.main()

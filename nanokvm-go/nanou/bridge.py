"""Connect NanoU's private Unix sockets to the NanoKVM Go firmware."""

import hashlib
import hmac
import http.client
import json
import socket
import ssl
import struct
import threading
import time
from contextlib import closing
from http.cookies import SimpleCookie

import websocket

PORT = 443
MAX_FRAME = 8 * 1024 * 1024
TIMEOUT = 4
VIDEO_PROBLEMS = {
    "-1": "No video signal from the attached computer",
    "-2": "NanoKVM video encoder error",
    "-4": "NanoKVM video mode changed",
    "-5": "NanoKVM video capture is disabled",
}
POINTER_EVENTS = {"absolute", "relative", "button", "button_click", "scroll"}


class FirmwareError(RuntimeError):
    pass


def firmware_tls(host, pin, raw=None, *, port=PORT):
    # Go uses a self-signed leaf. Check its fingerprint before any HTTP data is sent.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    tcp = raw or socket.create_connection((host, port), timeout=TIMEOUT)
    try:
        connection = context.wrap_socket(tcp, server_hostname=host)
        actual = hashlib.sha256(connection.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(pin, actual):
            connection.close()
            raise FirmwareError("The KVM certificate fingerprint changed.")
        return connection
    except BaseException:
        tcp.close()
        raise


class Firmware:
    def __init__(self, pin, host="127.0.0.1", port=PORT):
        self.host = host
        self.port = port
        address = f"[{host}]" if ":" in host else host
        self.authority = address if port == PORT else f"{address}:{port}"
        self.pin = pin.strip().lower().replace(":", "")
        if len(self.pin) != 64 or any(value not in "0123456789abcdef" for value in self.pin):
            raise FirmwareError("Enter the SHA-256 fingerprint of the KVM TLS certificate.")
        self.cookie = ""
        self._credentials = None
        self._login_lock = threading.Lock()

    def tls(self):
        return firmware_tls(self.host, self.pin, port=self.port)

    def post(self, path, body, *, use_cookie=True):
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=TIMEOUT)
        with closing(conn):
            conn.sock = self.tls()
            headers = {"Content-Type": "application/json", "Origin": f"https://{self.authority}"}
            if use_cookie and self.cookie:
                headers["Cookie"] = self.cookie
            conn.request("POST", path, json.dumps(body), headers)
            response = conn.getresponse()
            if response.status != 200:
                raise FirmwareError(f"Firmware rejected the request (HTTP {response.status}).")
            payload = json.loads(response.read(64 * 1024))
            if payload.get("code") != 0:
                raise FirmwareError("Firmware rejected the request. Check the local KVM account.")
            cookies = SimpleCookie()
            for name, value in response.getheaders():
                if name.lower() == "set-cookie":
                    cookies.load(value)
            if cookies or not use_cookie:
                self.cookie = "; ".join(morsel.OutputString().split(";", 1)[0] for morsel in cookies.values())

    def login(self, username, password):
        with self._login_lock:
            self._login(username, password)
            self._credentials = (username, password)

    def _login(self, username, password):
        self.post("/api/auth/login", {"username": username, "password": password}, use_cookie=False)
        if not self.cookie:
            raise FirmwareError("The KVM did not issue a session. Check if two-factor login is required.")

    def ws(self, path):
        cookie = self.cookie
        try:
            return self._ws(path, cookie)
        except websocket.WebSocketBadStatusException as error:
            if error.status_code != 401 or self._credentials is None:
                raise
        # A background host can outlive its firmware session. Refresh once even
        # if several relays see the same expired cookie at the same time.
        with self._login_lock:
            if self.cookie == cookie:
                self._login(*self._credentials)
            cookie = self.cookie
        return self._ws(path, cookie)

    def _ws(self, path, cookie):
        peer = self.tls()
        try:
            return websocket.create_connection(
                f"wss://{self.authority}{path}",
                socket=peer,
                cookie=cookie,
                origin=f"https://{self.authority}",
                timeout=TIMEOUT,
                redirect_limit=0,
                enable_multithread=True,
            )
        except BaseException:
            peer.close()
            raise


VK_TO_HID = {
    **{key: key - 0x41 + 0x04 for key in range(0x41, 0x5B)},
    **{key: key - 0x31 + 0x1E for key in range(0x31, 0x3A)},
    **{key: key - 0x70 + 0x3A for key in range(0x70, 0x7C)},
    **{key: key - 0x61 + 0x59 for key in range(0x61, 0x6A)},
    0x30: 0x27, 0x0D: 0x28, 0x1B: 0x29, 0x08: 0x2A, 0x09: 0x2B,
    0x20: 0x2C, 0xBD: 0x2D, 0xBB: 0x2E, 0xDB: 0x2F, 0xDD: 0x30,
    0xDC: 0x31, 0xBA: 0x33, 0xDE: 0x34, 0xC0: 0x35, 0xBC: 0x36,
    0xBE: 0x37, 0xBF: 0x38, 0x14: 0x39, 0x2C: 0x46, 0x91: 0x47,
    0x13: 0x48, 0x2D: 0x49, 0x24: 0x4A, 0x21: 0x4B, 0x2E: 0x4C,
    0x23: 0x4D, 0x22: 0x4E, 0x27: 0x4F, 0x25: 0x50, 0x28: 0x51,
    0x26: 0x52, 0x90: 0x53, 0x6F: 0x54, 0x6A: 0x55, 0x6D: 0x56,
    0x6B: 0x57, 0x60: 0x62, 0x6E: 0x63, 0x5D: 0x65,
}
MODIFIERS = {
    0x11: 0, 0xA2: 0, 0x10: 1, 0xA0: 1, 0x12: 2, 0xA4: 2, 0x5B: 3,
    0xA3: 4, 0xA1: 5, 0xA5: 6, 0x5C: 7,
}


def signed_word(value):
    return max(-32768, min(32767, value))


class HID:
    def __init__(self, peer, sensitivity=100):
        self.peer = peer
        self.sequence = 0
        self.modifier = 0
        self.keys = set()
        self.buttons = 0
        self.absolute = None
        self.scroll_remainder = 0
        self.sensitivity = sensitivity
        self.motion_remainder = [0, 0]

    def emit(self, source, kind, payload):
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        millis = int(time.monotonic() * 1000) & 0xFFFFFFFF
        header = struct.pack("<BBBBBII", 3, 1, source, kind, 0, self.sequence, millis)
        self.peer.send_binary(header + payload)

    def keyboard(self):
        self.emit(1, 1, bytes([self.modifier, 0, *sorted(self.keys)[:6]]).ljust(8, b"\0"))

    def key(self, key, down):
        if key in MODIFIERS:
            mask = 1 << MODIFIERS[key]
            self.modifier = self.modifier | mask if down else self.modifier & ~mask
        elif key in VK_TO_HID:
            code = VK_TO_HID[key]
            self.keys.add(code) if down else self.keys.discard(code)
        else:
            return
        self.keyboard()

    def pointer(self, x=0, y=0, wheel=0, relative=False):
        wheel = signed_word(wheel)
        if self.absolute is not None and not relative:
            self.emit(2, 3, struct.pack("<BHHh", self.buttons, *self.absolute, wheel))
        else:
            self.emit(2, 2, struct.pack("<Bhhh", self.buttons, signed_word(x), signed_word(y), wheel))

    def reset(self):
        self.modifier = self.buttons = 0
        self.keys.clear()
        self.scroll_remainder = 0
        self.motion_remainder = [0, 0]
        self.keyboard()
        self.pointer()
        self.absolute = None

    def repair_iphone_drag(self):
        # Match the NanoKVM web console's two button-free wheel reports.
        virtual_pointer = 17
        self.emit(virtual_pointer, 3, struct.pack("<BHHh", 0, 32768, 32768, 1))
        time.sleep(0.030)
        self.emit(virtual_pointer, 3, struct.pack("<BHHh", 0, 32768, 32768, -1))

    def event(self, report, firmware):
        kind = report["event"]
        if kind == "key":
            self.key(report["key"], report["down"])
        elif kind == "key_click":
            self.key(report["key"], True)
            self.key(report["key"], False)
        elif kind in {"button", "button_click"}:
            mask = report["button"]
            if mask not in {1, 2, 4, 8, 16}:
                return
            if kind == "button_click" or report["down"]:
                self.buttons |= mask
            else:
                self.buttons &= ~mask
            self.pointer()
            if kind == "button_click":
                self.buttons &= ~mask
                self.pointer()
        elif kind == "absolute":
            self.absolute = tuple(round(max(0, min(1, report[key])) * 65535) for key in ("x", "y"))
            self.pointer()
        elif kind == "relative":
            self.absolute = None
            deltas = []
            for axis, key in enumerate(("x", "y")):
                scaled = report[key] * self.sensitivity + self.motion_remainder[axis]
                delta = abs(scaled) // 100
                delta = -delta if scaled < 0 else delta
                self.motion_remainder[axis] = scaled - delta * 100
                deltas.append(delta)
            if any(deltas):
                self.pointer(*deltas, relative=True)
        elif kind == "scroll":
            self.scroll_remainder += report["y"]
            steps = int(self.scroll_remainder / 120)
            self.scroll_remainder -= steps * 120
            if steps:
                self.pointer(wheel=steps)
        elif kind == "text":
            firmware.post("/api/hid/paste", {"content": report["text"]})
        elif kind == "reset":
            self.reset()


class Bridge:
    def __init__(self, firmware, directory, status, sensitivity=100, audio_key="", *, iphone_drag_repair=False):
        self.firmware = firmware
        self.directory = directory
        self.status = status
        self.sensitivity = sensitivity
        self.iphone_drag_repair = iphone_drag_repair
        self.iphone_video_recovered = threading.Event()
        self.stopping = threading.Event()
        self.video = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.input = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.audio = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) if audio_key else None
        self.audio_key = audio_key
        self.workers = []

    def __enter__(self):
        try:
            self.video.bind(str(self.directory / "video"))
            self.video.listen(1)
            self.input.bind(str(self.directory / "input"))
            workers = [(self.video, self.video_loop), (self.input, self.input_loop)]
            if self.audio is not None:
                self.audio.bind(str(self.directory / "audio"))
                self.audio.listen(1)
                workers.append((self.audio, self.audio_loop))
            for sock, worker in workers:
                sock.settimeout(0.25)
                thread = threading.Thread(target=worker, daemon=True)
                self.workers.append(thread)
                thread.start()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        self.stopping.set()
        self.video.close()
        self.input.close()
        if self.audio is not None:
            self.audio.close()
        for worker in self.workers:
            worker.join(timeout=TIMEOUT + 1)

    def video_loop(self):
        no_image = False
        while not self.stopping.is_set():
            try:
                client, _ = self.video.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            problem = None
            try:
                with client, closing(self.firmware.ws("/api/stream/h264/direct")) as upstream:
                    client.settimeout(TIMEOUT)
                    started = time.monotonic()
                    active = False
                    while not self.stopping.is_set():
                        frame = upstream.recv()
                        if isinstance(frame, bytes) and 10 <= len(frame) <= MAX_FRAME:
                            problem = None
                            if no_image:
                                self.iphone_video_recovered.set()
                                no_image = False
                            client.sendall(struct.pack(">I", len(frame)) + frame)
                            if not active:
                                self.status("Streaming NanoKVM")
                                active = True
                        else:
                            if isinstance(frame, str) and frame:
                                try:
                                    notice = json.loads(frame)
                                except ValueError:
                                    notice = None
                                if isinstance(notice, dict) and notice.get("event") == "video-status":
                                    code = notice.get("data")
                                    if isinstance(code, str):
                                        problem = VIDEO_PROBLEMS.get(code)
                                        if self.iphone_drag_repair and code == "-1":
                                            no_image = True
                            if not frame or time.monotonic() - started > TIMEOUT:
                                raise FirmwareError("No direct H.264 video")
            except (BrokenPipeError, ConnectionResetError):
                self.status("Ready for UU")
            except (OSError, websocket.WebSocketException, FirmwareError):
                if not self.stopping.is_set():
                    self.status(problem or "KVM video unavailable")

    def audio_loop(self):
        from audio import relay_speaker

        while not self.stopping.is_set():
            try:
                client, _ = self.audio.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                with client:
                    relay_speaker(self.firmware, self.audio_key, client, self.stopping)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (OSError, ValueError, KeyError, IndexError, websocket.WebSocketException, FirmwareError):
                if not self.stopping.is_set():
                    self.status("Audio unavailable. Check MCP and Virtual Audio")

    def input_loop(self):
        peer = None
        heartbeat_at = 0.0
        repair_pending = False
        try:
            while not self.stopping.is_set():
                try:
                    raw = self.input.recv(65536)
                except socket.timeout:
                    raw = None
                except OSError:
                    break
                try:
                    if raw is None:
                        if peer and time.monotonic() >= heartbeat_at:
                            peer.send_binary(b"\0")
                            heartbeat_at = time.monotonic() + 10
                        continue
                    report = json.loads(raw)
                    kind = report.get("event")
                    if peer is None:
                        if kind == "reset":
                            continue
                        peer = self.firmware.ws("/api/ws")
                        hid = HID(peer, self.sensitivity)
                        heartbeat_at = time.monotonic() + 10
                        repair_pending = self.iphone_drag_repair
                    if (kind in POINTER_EVENTS and not hid.buttons
                            and (repair_pending or self.iphone_video_recovered.is_set())):
                        self.iphone_video_recovered.clear()
                        hid.repair_iphone_drag()
                        repair_pending = False
                    hid.event(report, self.firmware)
                    if kind == "reset":
                        peer.close()
                        peer = None
                except (OSError, ValueError, KeyError, websocket.WebSocketException, FirmwareError):
                    if peer:
                        peer.close()
                        peer = None
                    if not self.stopping.is_set():
                        self.status("KVM input unavailable")
        except (OSError, websocket.WebSocketException):
            if not self.stopping.is_set():
                self.status("KVM input unavailable")
        finally:
            if peer:
                try:
                    hid.reset()
                except (OSError, websocket.WebSocketException):
                    pass
                peer.close()

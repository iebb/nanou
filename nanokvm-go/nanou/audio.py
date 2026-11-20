"""Relay stock NanoKVM Go speaker WebRTC signaling to the local UU host."""

import http.client
import json
import select
import socket
import time
from contextlib import closing
from urllib.parse import urlsplit

import websocket

from bridge import FirmwareError

MAX_SIGNAL = 128 * 1024
MEDIA_TTL = 600


class SpeakerSession:
    def __init__(self, firmware, api_key):
        self.firmware = firmware
        self.api_key = api_key
        self.session = ""
        self.request_id = 0
        self.token = ""

    def request(self, method, arguments, notification=False):
        self.request_id += 1
        body = {"jsonrpc": "2.0", "method": method, "params": arguments}
        if not notification:
            body["id"] = self.request_id
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        conn = http.client.HTTPSConnection(self.firmware.host, self.firmware.port, timeout=4)
        with closing(conn):
            conn.sock = self.firmware.tls()
            conn.request("POST", "/api/mcp", json.dumps(body), headers)
            response = conn.getresponse()
            if response.status not in {200, 202, 204}:
                raise FirmwareError("Check Virtual Audio, MCP Service, and the MCP key")
            if method == "initialize":
                self.session = response.getheader("Mcp-Session-Id", "")
            if notification:
                return
            if (response.getheader("Content-Type") or "").startswith("text/event-stream"):
                for _ in range(32):
                    line = response.readline(MAX_SIGNAL)
                    if line.startswith(b"data:"):
                        payload = json.loads(line[5:])
                        break
                    if not line:
                        raise FirmwareError("Audio session returned no result")
                else:
                    raise FirmwareError("Audio session returned no result")
            else:
                payload = json.loads(response.read(MAX_SIGNAL))
        result = payload.get("result")
        if payload.get("error") or not isinstance(result, dict) or result.get("isError"):
            raise FirmwareError("NanoKVM audio session failed")
        return result

    def __enter__(self):
        self.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "NanoU", "version": "0.3.1"},
        })
        self.request("notifications/initialized", {}, notification=True)
        result = self.request("tools/call", {"name": "media_session_create", "arguments": {
            "speakerReceive": True, "micSend": False, "ttlSeconds": MEDIA_TTL,
        }})
        try:
            media = result.get("structuredContent") or json.loads(result["content"][0]["text"])
            self.token = media["token"]
            self.path = media["signalingPath"]
            url = urlsplit(self.path)
            if not self.token or url.scheme or url.netloc or url.path != "/api/media/audio/webrtc":
                raise FirmwareError("Unexpected NanoKVM audio signaling address")
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        if self.token:
            try:
                self.request("tools/call", {"name": "media_session_close", "arguments": {"token": self.token}})
            except (OSError, ValueError, FirmwareError):
                pass
            self.token = ""


def relay_speaker(firmware, key, client, stopping):
    with SpeakerSession(firmware, key) as media, closing(firmware.ws(media.path)) as upstream:
        client.settimeout(4)
        deadline = time.monotonic() + MEDIA_TTL - 15
        pending = bytearray()
        while not stopping.is_set() and time.monotonic() < deadline:
            readable, _, _ = select.select([client, upstream.sock], [], [], 0.25)
            if client in readable:
                chunk = client.recv(65536)
                if not chunk:
                    return
                pending.extend(chunk)
                while (index := pending.find(b"\n")) >= 0:
                    if index > MAX_SIGNAL:
                        raise FirmwareError("Audio signaling message exceeds size limit")
                    upstream.send(bytes(pending[:index]).decode("utf-8"))
                    del pending[:index + 1]
                if len(pending) > MAX_SIGNAL:
                    raise FirmwareError("Audio signaling message exceeds size limit")
            if upstream.sock in readable or getattr(upstream.sock, "pending", lambda: 0)():
                try:
                    message = upstream.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not message:
                    return
                if isinstance(message, str):
                    data = json.loads(message)
                    if data.get("event") in {"answer", "candidate"}:
                        client.sendall(json.dumps(data, separators=(",", ":")).encode() + b"\n")

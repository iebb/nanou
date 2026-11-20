"""Connect one NanoKVM console over a UU tunnel."""

import asyncio
import re

from uu_tunnel.auth.store import clear_saved_auth, load_client, save_client
from uu_tunnel.core.forward import ForwardAcceptor
from uu_tunnel.core.manager import TunnelManager

from .config import WEB_PORTS

LOOPBACK = "127.0.0.1"
CONSOLE_ROUTE = "nanokvm"


def gateway_acceptor(destination):
    route = (CONSOLE_ROUTE, WEB_PORTS[destination.scheme])
    return ForwardAcceptor(
        validate_target=lambda host, port: (host, port) == route,
        fixed_destination=(destination.host, destination.port),
    )


def check_phone(phone):
    if not isinstance(phone, str) or not re.fullmatch(r"[0-9]{11}", phone):
        raise ValueError("Enter an 11-digit mainland China mobile number.")
    return phone


def check_code(code):
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{4,8}", code):
        raise ValueError("Enter the numeric verification code from your SMS.")
    return code


class Controller:
    """Own the account and one outbound tunnel for this app process."""

    def __init__(self):
        self.client = load_client()
        self.manager = TunnelManager(
            self.client,
            forward_acceptor=ForwardAcceptor(validate_target=lambda _host, _port: False),
            report=lambda _message: None,
        )
        self.lock = asyncio.Lock()
        self.session = None
        self.sms_phone = None

    def status(self):
        if self.session and not any(
            item.peer_key == self.session["device_id"] and item.status == "connected"
            for item in self.manager.tunnels()
        ):
            self.session = None
        return {"signed_in": self.client.logged_in, "session": self.session}

    async def send_sms(self, phone):
        check_phone(phone)
        self.client.set_device_name("NanoU controller")
        self.sms_phone = None
        await self.client.send_sms_code("86", phone)
        self.sms_phone = phone

    async def sign_in(self, phone, code):
        check_phone(phone)
        check_code(code)
        if phone != self.sms_phone:
            raise ValueError("Request a verification code for this number first.")
        await self.client.login_by_mobile("86", phone, code)
        save_client(self.client)
        self.sms_phone = None

    async def sign_out(self):
        await self.disconnect()
        clear_saved_auth()
        self.client.clear_credentials()
        self.sms_phone = None

    async def devices(self):
        return [
            {
                "id": record["device_id"],
                "name": record["alias"] or record["device_id"],
                "online": record["online"],
                "available": record["online"]
                and record["can_be_controlled"]
                and not record["occupied"],
            }
            for record in await self.client.list_devices()
            if record["device_id"] and not record["shared"]
        ]

    async def connect(self, device_id, scheme):
        if not isinstance(device_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", device_id):
            raise ValueError("Select an available gateway.")
        if not isinstance(scheme, str) or scheme not in WEB_PORTS:
            raise ValueError("Select HTTP or HTTPS to match the gateway.")
        if self.status()["session"]:
            raise ValueError("Disconnect the current gateway before opening another.")
        devices = await self.devices()
        device = next((entry for entry in devices if entry["id"] == device_id), None)
        if not device or not device["available"]:
            raise ValueError("The gateway is unavailable. Refresh the device list and try again.")

        tunnel = await self.manager.connect_device(device_id)
        try:
            forward = await self.manager.add_forward(
                tunnel.peer_key,
                listen_port=0,
                target_host=CONSOLE_ROUTE,
                target_port=WEB_PORTS[scheme],
            )
        except BaseException:
            await self.manager.disconnect(tunnel.peer_key)
            raise

        self.session = {
            "device_id": tunnel.peer_key,
            "name": device["name"],
            "url": f"{scheme}://{LOOPBACK}:{forward.listen_port}/",
        }
        return self.session

    async def disconnect(self):
        if self.session:
            await self.manager.disconnect(self.session["device_id"])
            self.session = None

    async def close(self):
        try:
            await self.manager.stop()
        finally:
            await self.client.close()

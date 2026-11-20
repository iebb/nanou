"""Loopback-only browser UI for NanoU."""

from pathlib import Path

from aiohttp import web

from uu_tunnel.api import UuApiError, UuAuthExpiredError
from uu_tunnel.webrtc import TunnelSessionError

from .transport import Controller

STATIC = Path(__file__).with_name("static")
ACTIONS = {
    "sms": ("send_sms", ("phone",)),
    "sign-in": ("sign_in", ("phone", "code")),
    "sign-out": ("sign_out", ()),
    "devices": ("devices", ()),
    "connect": ("connect", ("device_id", "scheme")),
    "disconnect": ("disconnect", ()),
}
FAILURE_MESSAGES = {
    "sms": "UU could not send a code. Check your number or wait before retrying.",
    "sign-in": "UU sign-in failed. Check the code or request a new one.",
    "devices": "UU could not load your devices. Check your connection and refresh.",
    "connect": "The UU connection could not open. Check that NanoU Gateway is running and retry.",
}
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"


def create_app(controller=None):
    controller = controller if controller is not None else Controller()

    @web.middleware
    async def local_only(request, handler):
        socket_address = request.transport.get_extra_info("sockname")
        host = f"{socket_address[0]}:{socket_address[1]}"
        if request.host != host:
            raise web.HTTPForbidden()
        if request.method == "POST" and (
            request.headers.get("Origin") != f"http://{host}"
            or request.content_type != "application/json"
        ):
            raise web.HTTPForbidden()
        response = await handler(request)
        response.headers.update({
            "Cache-Control": "no-store",
            "Content-Security-Policy": CSP,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cross-Origin-Resource-Policy": "same-origin",
        })
        return response

    async def asset(request):
        name = request.match_info.get("file", "index.html")
        if name not in {"index.html", "app.js", "app.css"}:
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / name)

    async def status(_request):
        return web.json_response(controller.status())

    async def action(request):
        name = request.match_info["action"]
        if name not in ACTIONS:
            raise web.HTTPNotFound()
        method, keys = ACTIONS[name]
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != set(keys):
                raise ValueError("Invalid request.")
        except ValueError:
            return web.json_response({"error": "Invalid request."}, status=400)

        async with controller.lock:
            signed_in = controller.status()["signed_in"]
            signing_in = name in {"sms", "sign-in"}
            if signed_in == signing_in:
                return web.json_response({"error": "Refresh to use your current UU sign-in."}, status=409)
            try:
                result = await getattr(controller, method)(*(body[key] for key in keys))
            except ValueError as exc:
                return web.json_response({"error": str(exc)}, status=400)
            except UuAuthExpiredError:
                await controller.sign_out()
                return web.json_response(
                    {"error": "Your UU session expired. Sign in again.", **controller.status()},
                    status=401,
                )
            except (UuApiError, TunnelSessionError, OSError, TimeoutError):
                message = FAILURE_MESSAGES.get(name, "The operation failed. Try again.")
                return web.json_response({"error": message}, status=502)
            return web.json_response({"result": result, **controller.status()})

    async def cleanup(_app):
        await controller.close()

    app = web.Application(middlewares=[local_only], client_max_size=8192)
    app.router.add_get("/", asset)
    app.router.add_get("/static/{file}", asset)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/{action}", action)
    app.on_cleanup.append(cleanup)
    return app

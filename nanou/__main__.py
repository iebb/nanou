"""Run the NanoU controller or the gateway beside a NanoKVM."""

import argparse
import asyncio
import getpass
import os
import sys
import webbrowser

from .config import Destination, state_directory


async def run_controller(open_browser):
    from aiohttp import web

    from .transport import LOOPBACK
    from .web import create_app

    runner = web.AppRunner(create_app(), access_log=None, shutdown_timeout=2)
    await runner.setup()
    try:
        site = web.TCPSite(runner, LOOPBACK, 0)
        await site.start()
        address, port = runner.addresses[0][:2]
        url = f"http://{address}:{port}/"
        print(f"NanoU is open at {url}", flush=True)
        if open_browser:
            await asyncio.to_thread(webbrowser.open, url)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def run_gateway(destination, name):
    from uu_tunnel.api import UuAuthExpiredError
    from uu_tunnel.auth.store import clear_saved_auth, load_client, save_client
    from uu_tunnel.core.events import TunnelClosed, TunnelEstablished
    from uu_tunnel.core.manager import TunnelManager

    from .transport import check_code, check_phone, gateway_acceptor

    client = load_client()
    manager = None
    try:
        if not client.logged_in:
            client.set_device_name(name or "NanoU Gateway")
            phone = check_phone(input("UU mobile number (+86): ").strip())
            await client.send_sms_code("86", phone)
            code = check_code(getpass.getpass("SMS verification code: ").strip())
            await client.login_by_mobile("86", phone, code)
            save_client(client)
        if name and name != client.state.get("device_name"):
            await client.set_device_alias(name)
            save_client(client)

        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(destination.host, destination.port), timeout=4
            )
            writer.close()
            await writer.wait_closed()
        except (OSError, TimeoutError):
            print("NanoKVM is unreachable from this gateway. Check its address and network.", file=sys.stderr)

        manager = TunnelManager(client, forward_acceptor=gateway_acceptor(destination))
        print(f"Gateway {client.device_id} is starting. Select {destination.scheme.upper()} in NanoU.", flush=True)

        def report(event):
            if isinstance(event, TunnelEstablished):
                print("NanoU controller connected.", flush=True)
            elif isinstance(event, TunnelClosed):
                print("NanoU controller disconnected.", flush=True)

        manager.events.subscribe(report)
        await manager.serve_account()
    except UuAuthExpiredError:
        clear_saved_auth()
        raise ValueError("Gateway sign-in expired. Start it again to sign in by SMS.") from None
    finally:
        try:
            if manager is not None:
                await manager.stop()
        finally:
            await client.close()


def main():
    parser = argparse.ArgumentParser(prog="NanoU", description="Open a NanoKVM console over a UU tunnel.")
    commands = parser.add_subparsers(dest="role")
    controller = commands.add_parser("controller", help="Open the NanoU browser app (default).")
    controller.add_argument("--no-browser", action="store_true", help="Only print the local app URL.")
    gateway = commands.add_parser("gateway", help="Run beside the NanoKVM.")
    gateway.add_argument("url", help="NanoKVM base URL as seen from this host.")
    gateway.add_argument("--name", help="UU device name for this gateway.")
    native = commands.add_parser("native", help="Stream NanoKVM to the official UU app from Linux.")
    native.add_argument("url", help="NanoKVM base URL as seen from this host.")
    commands.add_parser("native-login", help="Enroll the Linux UU host by scanning a QR code.")
    args = parser.parse_args()
    role = args.role or "controller"
    if role in {"native", "native-login"}:
        from .native import launch

        return launch(args)
    try:
        from uu_tunnel.api import UuApiError
        from uu_tunnel.webrtc import TunnelSessionError
    except ImportError:
        print("Install the browser tunnel dependencies with uv sync --locked.", file=sys.stderr)
        return 1
    state = state_directory()
    os.environ["UU_TUNNEL_CONFIG"] = str(state / f"{role}.json")
    try:
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        if role == "gateway":
            asyncio.run(run_gateway(Destination.from_url(args.url), args.name))
        else:
            asyncio.run(run_controller(not getattr(args, "no_browser", False)))
    except KeyboardInterrupt:
        return 0
    except UuApiError:
        print("UU could not complete the gateway operation. Check its network and sign-in.", file=sys.stderr)
        return 1
    except (EOFError, OSError, TunnelSessionError, ValueError) as exc:
        print(f"NanoU: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

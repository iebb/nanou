#!/usr/bin/env python3
"""Run a browserless NanoKVM Go bridge on a Linux machine."""

import argparse
import getpass
import os
import platform
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

from runtime import ROOT, FirmwareError, HostRuntime, enrolled, run_host, stop
from settings import Settings, device_name

LOADERS = {"aarch64": "ld-linux-aarch64.so.1", "x86_64": "ld-linux-x86-64.so.2"}


def target(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.path not in ("", "/") or "?" in url or "#" in url
        or parsed.port == 0 or any(c.isspace() or ord(c) < 32 for c in url)
    ):
        raise ValueError("Enter the KVM HTTPS base URL, such as https://192.168.1.42")
    return parsed.hostname, parsed.port or 443


def parser():
    cli = argparse.ArgumentParser(prog="nanou-bridge", description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="Enroll in UU Remote with a terminal QR code")
    login.add_argument("--name", help="Device name shown in UU Remote")
    run = commands.add_parser("run", help="Stream the KVM to the official UU Remote app")
    run.add_argument("url", help="NanoKVM Go HTTPS base URL")
    run.add_argument("--tls-pin", help="Verified SHA-256 certificate fingerprint")
    run.add_argument("--username", help="Local KVM username")
    run.add_argument("--name", help="Device name shown in UU Remote")
    run.add_argument("--mouse-sensitivity", help="Relative motion scale in percent")
    run.add_argument("--iphone-drag-repair", action="store_true", help="Repair touch-as-mouse input on an attached iPhone")
    run.add_argument("--click-to-touch", action="store_true", help="Send touchscreen taps and drags from absolute pointer input")
    run.add_argument("--audio", action="store_true", help="Forward KVM audio using NANOU_KVM_MCP_KEY")
    return cli


def settings_from_args(args):
    config = dict(os.environ)
    for name, value in (
        ("NANOU_KVM_USER", args.username),
        ("NANOU_KVM_TLS_PIN", args.tls_pin),
        ("NANOU_UU_NAME", args.name),
        ("NANOU_MOUSE_SENSITIVITY", args.mouse_sensitivity),
    ):
        if value is not None:
            config[name] = value
    if not config.get("NANOU_KVM_TLS_PIN"):
        raise ValueError("Provide the verified KVM certificate fingerprint with --tls-pin")
    config.setdefault("NANOU_KVM_USER", "admin")
    if args.audio:
        config["NANOU_AUDIO"] = "true"
    if args.iphone_drag_repair:
        config["NANOU_IPHONE_DRAG_REPAIR"] = "true"
    if args.click_to_touch:
        config["NANOU_CLICK_TO_TOUCH"] = "true"
    if not config.get("NANOU_KVM_PASSWORD"):
        if not sys.stdin.isatty():
            raise ValueError("Set NANOU_KVM_PASSWORD or run with a terminal to enter it")
        config["NANOU_KVM_PASSWORD"] = getpass.getpass("NanoKVM password: ")
    return Settings.from_env(config)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if sys.platform != "linux" or os.geteuid() == 0:
            raise ValueError("Run the bridge as a regular user on Linux")
        loader = LOADERS.get(platform.machine())
        if loader is None:
            raise ValueError("The Linux bridge supports x86-64 and ARM64")
        state = Path(os.environ.get("NANOU_STATE_DIR", Path.home() / ".nanou")).expanduser().resolve()
        runtime = HostRuntime(ROOT, state, state / "native-uu.json", state, loader)
        runtime.command()  # Detect the wrong architecture bundle before prompting for credentials.
        cancel = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_args: cancel.set())
        signal.signal(signal.SIGINT, lambda *_args: cancel.set())
        with tempfile.TemporaryDirectory(prefix="nanou-", dir="/tmp") as scratch:
            directory = Path(scratch)
            if args.command == "login":
                name = device_name(args.name if args.name is not None else os.getenv("NANOU_UU_NAME", ""))
                with runtime.lock():
                    child = subprocess.Popen(runtime.command("login", "--no-gui"),
                                             env=runtime.environment(directory, name))
                    try:
                        while child.poll() is None and not cancel.wait(0.25):
                            pass
                        return 0 if cancel.is_set() or (child.returncode == 0 and enrolled(runtime)) else 1
                    finally:
                        stop(child)
            if not enrolled(runtime):
                raise ValueError("Run nanou-bridge login first")
            address = target(args.url)
            options = settings_from_args(args)
            return 0 if run_host(options, directory, cancel, lambda message: print(message, flush=True),
                                 runtime=runtime, address=address) else 1
    except KeyboardInterrupt:
        return 0
    except (FirmwareError, OSError, ValueError) as error:
        print(f"NanoU: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

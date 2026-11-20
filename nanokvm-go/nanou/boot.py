"""Opt-in systemd startup for NanoU without a framebuffer session."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path

from runtime import ROOT, SERVICE_DIR, run_host
from settings import Settings

UNIT = "nanou-kvm.service"
CONFIG = SERVICE_DIR / "settings.json"
UNIT_PATH = SERVICE_DIR / UNIT


def systemd(*args):
    subprocess.run(["systemctl", *args], check=True, timeout=15,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def quote_unit(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def write_private(path, text):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        output.write(text)
        temp = Path(output.name)
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def enable(settings):
    if os.geteuid() != 0 or not Path("/run/systemd/system").is_dir():
        raise OSError("Boot startup requires systemd on the NanoKVM")
    SERVICE_DIR.mkdir(mode=0o700, exist_ok=True)
    config = json.dumps(asdict(settings), ensure_ascii=False)
    previous = CONFIG.read_text() if CONFIG.exists() else None
    if previous != config:
        write_private(CONFIG, config)
    executable = quote_unit(sys.executable)
    launcher = quote_unit(ROOT / "main.py")
    unit = (
        "[Unit]\nDescription=NanoU for NanoKVM Go\nAfter=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\nType=simple\n"
        f"ExecCondition=/usr/bin/test -f {quote_unit(CONFIG)}\n"
        f"ExecStart={executable} {launcher} --service\n"
        "UMask=0077\nRestart=on-failure\nRestartSec=10s\nTimeoutStopSec=20s\n\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
    installed = UNIT_PATH.exists()
    unit_changed = not installed or UNIT_PATH.read_text() != unit
    if unit_changed:
        write_private(UNIT_PATH, unit)
        UNIT_PATH.chmod(0o644)
        systemd("daemon-reload")
    systemd("enable", str(UNIT_PATH))
    systemd("restart" if installed and (previous != config or unit_changed) else "start", UNIT)


def disable():
    if UNIT_PATH.exists() or CONFIG.exists():
        systemd("disable", "--now", UNIT)
        CONFIG.unlink(missing_ok=True)
        UNIT_PATH.unlink(missing_ok=True)
        systemd("daemon-reload")


def serve():
    try:
        settings = Settings(**json.loads(CONFIG.read_text()))
    except (OSError, TypeError, ValueError):
        return 0
    if not settings.autostart:
        return 0
    cancel = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_args: cancel.set())
    signal.signal(signal.SIGINT, lambda *_args: cancel.set())
    with tempfile.TemporaryDirectory(prefix="nanou-", dir="/tmp") as directory:
        return 0 if run_host(settings, Path(directory), cancel, lambda msg: print(msg, flush=True)) else 1

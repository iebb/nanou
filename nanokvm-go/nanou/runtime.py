"""Shared lifecycle for touchscreen and background NanoU sessions."""

import fcntl
import json
import os
import pwd
import subprocess
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
CONFIG = STATE / "uu.json"
SERVICE_DIR = ROOT / ".nanou-service"
sys.path.insert(0, str(ROOT / "python"))

from bridge import Bridge, Firmware, FirmwareError  # noqa: E402


def enrolled():
    try:
        data = json.loads(CONFIG.read_text())
        return isinstance(data, dict) and bool(data.get("token") and data.get("device_id"))
    except (OSError, ValueError):
        return False


def stop(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def run_host(settings, directory, cancel, report, *, can_enroll=False, link_only=False):
    """Return success after linking or intentional shutdown."""
    STATE.mkdir(mode=0o700, exist_ok=True)
    SERVICE_DIR.mkdir(mode=0o700, exist_ok=True)
    with closing((SERVICE_DIR / "host.lock").open("a")) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            report("NanoU is already running")
            return True
        return _run_locked(settings, directory, cancel, report, can_enroll, link_only)


def _run_locked(settings, directory, cancel, report, can_enroll, link_only):
    host = ROOT / "bin" / "uuremote-server"
    libraries = ROOT / "lib"
    loader = libraries / "ld-linux-armhf.so.3"
    qr_path = directory / "login.qr"
    identity = {}
    owner = None
    if os.geteuid() == 0:
        account = pwd.getpwnam("nobody")
        owner = (account.pw_uid, account.pw_gid)
        os.chown(STATE, *owner)
        os.chown(directory, *owner)
        identity = {"user": owner[0], "group": owner[1], "extra_groups": []}

    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("NANOU_", "UU_", "LD_", "RUST_LOG"))}
    env.update({
        "XDG_CONFIG_HOME": str(STATE),
        "UU_CONFIG": str(CONFIG),
        "UU_LANG": "en",
        "UU_NO_GTK": "1",
        "UU_AUDIO": "0",
        "UU_REMOTE_ASSIST": "0",
        "UU_PORT_MAPPING": "0",
        "UU_WOL_HELPER": "0",
        "UU_WALLPAPER_UPLOAD": "0",
        "UU_LOGIN_WINDOW": "0",
        "UU_DESKTOP_BACKEND": "x11",
        "UU_DESKTOP_RESIZE": "0",
        "UU_INPUT_BACKEND": "native",
        "UU_VIDEO_CODEC": "h264",
        "UU_VIDEO_ENCODER": "cpu",
        "NANOU_DEVICE_SOCKET_DIR": str(directory),
        "NANOU_LOGIN_QR_MATRIX": str(qr_path),
        "NANOU_UU_NAME": settings.name,
        "TOKIO_WORKER_THREADS": "2",
        "RUST_LOG": "warn",
    })

    def run_process(*args):
        child = subprocess.Popen(
            [str(loader), "--library-path", str(libraries), str(host), *args],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, **identity,
        )
        try:
            while child.poll() is None and not cancel.wait(0.25):
                if not (ROOT / "app.json").is_file():
                    cancel.set()
                    break
            return child.poll()
        finally:
            stop(child)

    try:
        if not host.is_file() or not loader.is_file():
            raise FirmwareError("ARM host binary missing")
        loader.chmod(0o755)
        firmware = Firmware(settings.tls_pin)
        firmware.login(settings.username, settings.password)
        if not enrolled():
            if not can_enroll:
                report("Open NanoU to pair with UU")
                return True
            report("Scan code in UU Remote")
            if run_process("login", "--no-gui") or not enrolled():
                if not cancel.is_set():
                    report("UU sign-in failed")
                return False
            qr_path.unlink(missing_ok=True)
        if link_only or cancel.is_set():
            return True
        audio_key = settings.mcp_key if settings.audio else ""
        with Bridge(firmware, directory, report, settings.sensitivity, audio_key) as bridge:
            if owner is not None:
                for name in ("video", "input", "audio") if bridge.audio else ("video", "input"):
                    os.chown(directory / name, *owner)
            if settings.audio and not audio_key:
                report("Video ready. Enter MCP key for audio")
            else:
                report("Open UU Remote on your client")
            run_process()
        if not cancel.is_set():
            report("UU host stopped")
            return False
        return True
    except FirmwareError as error:
        report(str(error))
        return not can_enroll
    except (OSError, ValueError):
        report("Local NanoU setup failed")
    return False

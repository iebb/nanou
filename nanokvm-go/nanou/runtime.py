"""Shared lifecycle for the SDK app and the standalone Linux bridge."""

import fcntl
import json
import os
import pwd
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
CONFIG = STATE / "uu.json"
SERVICE_DIR = ROOT / ".nanou-service"
sys.path.insert(0, str(ROOT / "python"))

from bridge import Bridge, Firmware, FirmwareError  # noqa: E402


class HostBusy(FirmwareError):
    pass


@dataclass(frozen=True)
class HostRuntime:
    bundle: Path
    state: Path
    config: Path
    lock_dir: Path
    loader_name: str
    sdk_manifest: Path | None = None

    def command(self, *args):
        host = self.bundle / "bin" / "uuremote-server"
        libraries = self.bundle / "lib"
        loader = libraries / self.loader_name
        if not host.is_file() or not loader.is_file():
            raise FirmwareError("The bundled UU host or its Linux runtime is missing")
        if self.sdk_manifest and not os.access(loader, os.X_OK):
            loader.chmod(0o755)
        return [str(loader), "--library-path", str(libraries), str(host), *args]

    @contextmanager
    def lock(self):
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.lock_dir / "host.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise HostBusy("NanoU is already running") from exc
            yield

    def environment(self, directory, name):
        display_vars = {"DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "GDK_BACKEND"}
        env = {key: value for key, value in os.environ.items()
               if key not in display_vars and not key.startswith(("NANOU_", "UU_", "LD_", "RUST_LOG"))}
        env.update({
            "XDG_CONFIG_HOME": str(self.state),
            "UU_CONFIG": str(self.config),
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
            "NANOU_LOGIN_QR_MATRIX": str(directory / "login.qr"),
            "NANOU_UU_NAME": name,
            "TOKIO_WORKER_THREADS": "2",
            "RUST_LOG": "warn",
        })
        return env


DEVICE = HostRuntime(ROOT, STATE, CONFIG, SERVICE_DIR, "ld-linux-armhf.so.3", ROOT / "app.json")


def enrolled(runtime=DEVICE):
    try:
        data = json.loads(runtime.config.read_text())
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


def run_host(settings, directory, cancel, report, *, can_enroll=False, link_only=False,
             runtime=DEVICE, address=("127.0.0.1", 443)):
    """Return success after linking or intentional shutdown."""
    try:
        with runtime.lock():
            return _run_locked(settings, directory, cancel, report, can_enroll, link_only, runtime, address)
    except HostBusy as error:
        report(str(error))
        return runtime.sdk_manifest is not None


def _run_locked(settings, directory, cancel, report, can_enroll, link_only, runtime, address):
    identity = {}
    owner = None
    if runtime.sdk_manifest and os.geteuid() == 0:
        account = pwd.getpwnam("nobody")
        owner = (account.pw_uid, account.pw_gid)
        os.chown(runtime.state, *owner)
        os.chown(directory, *owner)
        identity = {"user": owner[0], "group": owner[1], "extra_groups": []}

    env = runtime.environment(directory, settings.name)

    def run_process(*args):
        child = subprocess.Popen(
            runtime.command(*args), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL if runtime.sdk_manifest else None,
            **identity,
        )
        try:
            while child.poll() is None and not cancel.wait(0.25):
                if runtime.sdk_manifest and not runtime.sdk_manifest.is_file():
                    cancel.set()
                    break
            return child.poll()
        finally:
            stop(child)

    try:
        if not enrolled(runtime) and not can_enroll:
            report("Open NanoU to pair with UU" if runtime.sdk_manifest else "Run nanou-bridge login first")
            return runtime.sdk_manifest is not None
        firmware = Firmware(settings.tls_pin, *address)
        firmware.login(settings.username, settings.password)
        if not enrolled(runtime):
            report("Scan code in UU Remote")
            if run_process("login", "--no-gui") or not enrolled(runtime):
                if not cancel.is_set():
                    report("UU sign-in failed")
                return False
            (directory / "login.qr").unlink(missing_ok=True)
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
        return runtime.sdk_manifest is not None and not can_enroll
    except (OSError, ValueError):
        report("NanoU could not reach the KVM or start the UU host")
    return False

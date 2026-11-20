"""Run the UU-compatible Linux host on a dedicated NanoKVM display."""

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .config import Destination, state_directory

RESOLUTION = "1280x720"
WINDOW_CLASS = "NanoUKiosk"


def require_program(*names):
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    raise ValueError(f"Install {' or '.join(names)} before running NanoU native mode.")


def host_environment(state, base_env):
    return {
        **base_env,
        "UU_CONFIG": str(state / "native-uu.json"),
        "UU_LANG": "en",
        "UU_DESKTOP_BACKEND": "x11",
        "UU_DESKTOP_RESIZE": "0",
        "UU_INPUT_BACKEND": "native",
        "UU_LOGIN_WINDOW": "0",
        "UU_REMOTE_ASSIST": "0",
        "UU_PORT_MAPPING": "0",
        "UU_WOL_HELPER": "0",
        "UU_WALLPAPER_UPLOAD": "0",
    }


def launch(args):
    try:
        if sys.platform != "linux" or os.geteuid() == 0:
            raise ValueError("Run native mode as a regular user on a dedicated Linux host or VM.")
        host = require_program("uuremote-server")
        state = state_directory()
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = host_environment(state, os.environ)
        if args.role == "native-login":
            return subprocess.run([host, "login", "--no-gui"], env=env, check=False).returncode

        destination = Destination.from_url(args.url)
        if not Path(env["UU_CONFIG"]).is_file():
            raise ValueError("Run python3 -m nanou native-login --kiosk first.")
        xvfb_run = require_program("xvfb-run")
        require_program("Xvfb")
        require_program("xauth")
        require_program("ffmpeg")
        desktop = require_program("openbox")
        xdotool = require_program("xdotool")
        browser = require_program("chromium", "chromium-browser", "google-chrome")
        with socket.create_connection((destination.host, destination.port), timeout=5):
            pass
        env.pop("WAYLAND_DISPLAY", None)
        env["XDG_SESSION_TYPE"] = "x11"
        env["GDK_BACKEND"] = "x11"

        # xvfb-run owns the display and its disposable Xauthority cookie.
        os.execve(xvfb_run, [
            xvfb_run, "-a", "--server-args", f"-screen 0 {RESOLUTION}x24 -nolisten tcp",
            sys.executable, "-m", "nanou.native", args.url, host, desktop, xdotool, browser,
        ], env)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as exc:
        print(f"NanoU native: {exc}", file=sys.stderr)
        return 1


def stop_processes(processes):
    for child in reversed(processes):
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 4
    for child in reversed(processes):
        try:
            child.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        # A child can exit before its browser or encoder subprocesses.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def run_display(url, host, desktop, xdotool, browser, profile):
    processes = []

    def start(command):
        process = subprocess.Popen(command, start_new_session=True, stdin=subprocess.DEVNULL)
        processes.append(process)
        return process

    try:
        window_manager = start([desktop])
        kiosk = start([
            browser, "--ozone-platform=x11", f"--user-data-dir={profile}",
            f"--class={WINDOW_CLASS}", "--no-first-run", "--no-default-browser-check",
            "--disable-session-crashed-bubble", "--kiosk", url,
        ])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if window_manager.poll() is not None or kiosk.poll() is not None:
                raise ValueError("The NanoKVM browser or window manager exited during startup.")
            window = subprocess.run(
                [xdotool, "search", "--onlyvisible", "--class", f"^{WINDOW_CLASS}$"],
                capture_output=True, timeout=2, check=False,
            )
            if window.returncode == 0 and window.stdout.strip():
                break
            time.sleep(0.25)
        else:
            raise ValueError("The NanoKVM kiosk window did not appear.")

        server = start([host])
        print("NanoKVM kiosk started. Open the Linux host in the official UU app once it is online.", flush=True)
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
        if server.returncode is not None:
            raise ValueError("The UU host exited. Check its message above or run python3 -m nanou native-login again.")
        raise ValueError("The NanoKVM kiosk session closed.")
    finally:
        stop_processes(processes)


def stop_signal(_number, _frame):
    raise KeyboardInterrupt


def display_main():
    if sys.platform != "linux" or os.geteuid() == 0 or not os.environ.get("XAUTHORITY") or len(sys.argv) != 6:
        return 1
    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGHUP, stop_signal)
    try:
        with tempfile.TemporaryDirectory(prefix="nanou-chromium-") as profile:
            run_display(*sys.argv[1:], profile)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"NanoU native: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(display_main())

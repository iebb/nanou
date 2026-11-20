#!/usr/bin/env python3
"""NanoKVM Go touchscreen launcher and background service entry point."""

import subprocess
import sys
import tempfile
import threading
import textwrap
from pathlib import Path

import boot
from runtime import enrolled, run_host
from settings import Settings


def read_qr(path):
    try:
        rows = path.read_text().splitlines()
    except OSError:
        return []
    return rows if 21 <= len(rows) <= 177 and all(len(r) == len(rows) and set(r) <= {"0", "1"} for r in rows) else []


def draw_qr(ctx, rows, white, black):
    quiet = 4
    modules = len(rows) + quiet * 2
    unit = min(ctx.width - 16, ctx.height - 70) // modules
    if unit < 1:
        return
    size = unit * modules
    left = (ctx.width - size) // 2
    top = 42
    ctx.fb.fill_rect(left, top, size, size, white)
    for y, row in enumerate(rows):
        for x, cell in enumerate(row):
            if cell == "1":
                ctx.fb.fill_rect(left + (x + quiet) * unit, top + (y + quiet) * unit, unit, unit, black)


def touchscreen():
    from appbase import BLACK, WHITE, app, rgb565

    green = rgb565(189, 235, 124)
    gray = rgb565(151, 167, 153)

    @app()
    def main(ctx):
        status = "Starting NanoU"
        cancel = threading.Event()

        def report(value):
            nonlocal status
            status = value

        with tempfile.TemporaryDirectory(prefix="nanou-", dir="/tmp") as tmp:
            directory = Path(tmp)
            qr_path = directory / "login.qr"

            def launch():
                try:
                    settings = Settings.from_env(ctx.env)
                    if settings.autostart:
                        if not enrolled() and not run_host(
                            settings, directory, cancel, report, can_enroll=True, link_only=True
                        ):
                            return
                        if not cancel.is_set():
                            boot.enable(settings)
                            report("Running in background")
                    else:
                        boot.disable()
                        run_host(settings, directory, cancel, report, can_enroll=True)
                except ValueError as exc:
                    report(str(exc))
                except (OSError, subprocess.SubprocessError):
                    report("NanoU setup could not start")

            def tick(_elapsed):
                ctx.fb.clear(BLACK)
                ctx.fb.text_center(ctx.width // 2, 20, "NanoU", green, 2)
                qr = read_qr(qr_path)
                if qr:
                    draw_qr(ctx, qr, WHITE, BLACK)
                else:
                    for number, line in enumerate(textwrap.wrap(status, 24)[:3]):
                        ctx.fb.text_center(ctx.width // 2, ctx.height // 2 + number * 14, line, WHITE, 1)
                ctx.fb.text_center(ctx.width // 2, ctx.height - 15, "Hold left edge and swipe", gray, 1)

            worker = threading.Thread(target=launch, daemon=True)
            worker.start()
            try:
                ctx.run(tick, fps=5)
            finally:
                cancel.set()
                worker.join(timeout=15)

    main()


if __name__ == "__main__":
    if sys.argv[1:] == ["--service"]:
        raise SystemExit(boot.serve())
    touchscreen()

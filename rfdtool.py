#!/usr/bin/env python3
"""rfdtool entry point.

GUI:            python rfdtool.py [--port /dev/ttyUSB0 --baud 57600]
Flash .gbl:     python rfdtool.py --port /dev/ttyUSB0 --file firmware.gbl [--yes]
Detect only:    python rfdtool.py --port /dev/ttyUSB0 --dry-run

``--file``, ``--dry-run`` or ``--batch`` select the command-line flasher
(:mod:`rfd.gbl_flash`), which needs only pyserial + xmodem — Qt is never
imported on that path, so it runs on headless machines.
"""
from __future__ import annotations

import argparse
import sys

_FLASH_FLAGS = ("--file", "--dry-run", "--batch")


def _wants_flasher(argv: list[str]) -> bool:
    return any(a == f or a.startswith(f + "=") for a in argv for f in _FLASH_FLAGS)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="rfdtool",
        description="Linux GUI for configuring RFD900-series radio modems. "
                    "Add --file FIRMWARE.gbl (or --dry-run) to flash x-series "
                    "radios from the command line instead.",
    )
    p.add_argument(
        "--port",
        default=None,
        help="Serial port to auto-connect on launch (e.g. /dev/ttyUSB0).",
    )
    p.add_argument(
        "--baud",
        type=int,
        default=57600,
        help="Baud rate to use when --port is given (default: 57600).",
    )
    p.add_argument(
        "--no-mavlink",
        action="store_true",
        help="Disable MAVLink RADIO_STATUS parsing on the data stream.",
    )
    p.add_argument(
        "--skip-checks",
        action="store_true",
        help="Skip the startup system-environment checks.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    if _wants_flasher(raw):
        from rfd.gbl_flash import main as flash_main
        return flash_main(raw)

    args = parse_args(argv)

    from PySide6.QtWidgets import QApplication

    from rfd.radio import Radio
    from ui.main_window import MainWindow, run_startup_checks
    from ui.theme import apply_light_theme

    app = QApplication(sys.argv)
    app.setApplicationName("rfdtool")
    apply_light_theme(app)

    radio = Radio(mavlink_parsing=not args.no_mavlink)
    window = MainWindow(radio=radio)
    window.show()

    if not args.skip_checks:
        issues = run_startup_checks()
        if issues:
            window.show_system_issues(issues)

    if args.port:
        # Defer the auto-connect until the event loop is running so the UI
        # paints first; the radio's executor will handle the actual open.
        from PySide6.QtCore import QTimer

        QTimer.singleShot(100, lambda: radio.open_port(args.port, args.baud))

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

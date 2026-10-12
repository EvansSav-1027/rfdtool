"""Firmware update workflow for RFD900-series radios.

Lets the user pick a firmware file (.ihx / .hex / .bin), auto-detects which
uploader applies based on the radio's reported board ID, and drives the flash
through a worker thread so the UI thread stays responsive. The 8051 path
re-uses the radio's own pyserial handle (the bootloader continues speaking on
the same port at the same baud); the STM32 path closes the radio's port first
because ``stm32flash`` needs exclusive access.

Both paths share a single :class:`_FlashWorker` that lives on a :class:`QThread`
and emits ``progress(done, total)``, ``log(line)``, and ``finished(ok, msg)``
back to the GUI thread via queued connections. Cancellation is cooperative:
the Abort button flips a flag the worker polls between operations, which the
underlying uploaders translate into ``UploadCancelled`` / ``STM32FlashCancelled``.
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any, Optional

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from rfd.protocol import board_name, is_stm32_board
from rfd.radio import Radio


# File-extension classification. Lowercased extensions; ``.hex`` and ``.ihx``
# are both treated as the 8051 Intel-HEX format that ``upload_8051`` expects.
_IHX_EXTS = {".ihx", ".hex"}
_BIN_EXTS = {".bin"}


def _classify(path: str) -> str:
    """Return ``"8051"`` for Intel HEX, ``"stm32"`` for raw binary, ``""`` otherwise."""
    ext = os.path.splitext(path)[1].lower()
    if ext in _IHX_EXTS:
        return "8051"
    if ext in _BIN_EXTS:
        return "stm32"
    return ""


def _file_type_label(path: str) -> str:
    kind = _classify(path)
    if kind == "8051":
        return "Intel HEX (8051)"
    if kind == "stm32":
        return "Binary (STM32)"
    return "(unknown)"


def _human_bytes(n: int) -> str:
    """Format byte counts the way GUIs expect: thousands-separated, with a KB/MB tail."""
    if n < 1024:
        return f"{n:,} bytes"
    if n < 1024 * 1024:
        return f"{n:,} bytes ({n / 1024:.1f} KB)"
    return f"{n:,} bytes ({n / (1024 * 1024):.2f} MB)"


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

class _FlashWorker(QObject):
    """Runs the actual flash in a background thread.

    One worker is created per flash attempt and discarded when the thread
    finishes. ``run`` dispatches on ``self._kind`` (set in ``__init__``) and
    funnels both uploaders' progress / log callbacks through this object's
    Qt signals — those signals are connected with the default
    ``Qt.AutoConnection``, which becomes a queued connection across the
    thread boundary so the GUI thread is the only one touching widgets.
    """

    progress = Signal(int, int)     # bytes_done, total
    log = Signal(str)               # one log line, no trailing newline
    finished = Signal(bool, str)    # ok, message

    def __init__(
        self,
        kind: str,
        *,
        ser: Any = None,
        image: Any = None,
        port: str = "",
        bin_path: str = "",
        expected_board_id: int | None = None,
        baud: int = 57600,
    ) -> None:
        super().__init__()
        if kind not in ("8051", "stm32"):
            raise ValueError(f"unknown flash kind: {kind!r}")
        self._kind = kind
        self._ser = ser
        self._image = image
        self._port = port
        self._bin_path = bin_path
        self._expected_board_id = expected_board_id
        self._baud = baud
        self._cancel = False

    @Slot()
    def cancel(self) -> None:
        self._cancel = True

    def _cancel_check(self) -> bool:
        return self._cancel

    @Slot()
    def run(self) -> None:
        try:
            if self._kind == "8051":
                from rfd.uploader_8051 import upload_8051

                upload_8051(
                    self._ser,
                    self._image,
                    expected_board_id=self._expected_board_id,
                    progress=lambda d, t: self.progress.emit(d, t),
                    cancel_check=self._cancel_check,
                )
                self.finished.emit(True, "8051 flash complete")
            else:
                from rfd.uploader_stm32 import upload_stm32

                upload_stm32(
                    self._port,
                    self._bin_path,
                    baud=self._baud,
                    progress=lambda d, t: self.progress.emit(d, t),
                    cancel_check=self._cancel_check,
                    log=lambda line: self.log.emit(line),
                )
                self.finished.emit(True, "STM32 flash complete")
        except Exception as e:  # noqa: BLE001 — anything raised becomes a finished(False, ...)
            self.finished.emit(False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Tab widget
# ---------------------------------------------------------------------------

class FirmwareTab(QWidget):
    """Firmware update workflow."""

    status_message = Signal(str, int)   # text, timeout_ms

    def __init__(self, radio: Radio, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._radio = radio

        # Cached connection details. We mirror radio._port / radio._baud as
        # they're set, so we can reopen after the flash even when the radio
        # has closed its serial. ``radio.connected`` is the authoritative
        # signal — ``radio._port`` is the fallback.
        self._port: str = getattr(radio, "_port", "") or ""
        self._baud: int = int(getattr(radio, "_baud", 0) or 0)
        self._connected: bool = False

        # Cached identification. Kept dict-shaped to match ``radio_info``.
        self._radio_info: dict[str, Any] | None = None

        # Worker / thread are recreated for each flash.
        self._thread: Optional[QThread] = None
        self._worker: Optional[_FlashWorker] = None

        # Track in-progress state so we know whether to expect the radio to
        # reopen automatically or whether the user has to recover manually.
        self._flashing_kind: str = ""    # "" | "8051" | "stm32"

        self._build_ui()
        self._wire_radio()
        self._update_buttons()
        self._update_detected_label()

    # ------------------------------------------------------------------ UI build
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # ---- file picker + detection grid ------------------------------
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)

        grid.addWidget(QLabel("Firmware file:", self), 0, 0)
        self._path_edit = QLineEdit(self)
        self._path_edit.setPlaceholderText("Select an .ihx, .hex, or .bin firmware file…")
        self._path_edit.textChanged.connect(self._on_path_changed)
        grid.addWidget(self._path_edit, 0, 1)
        self._btn_browse = QPushButton("Browse…", self)
        self._btn_browse.clicked.connect(self._on_browse)
        grid.addWidget(self._btn_browse, 0, 2)

        grid.addWidget(QLabel("Detected board:", self), 1, 0)
        self._lbl_detected = QLabel("(connect to a radio first)", self)
        grid.addWidget(self._lbl_detected, 1, 1, 1, 2)

        grid.addWidget(QLabel("File type:", self), 2, 0)
        self._lbl_filetype = QLabel("—", self)
        grid.addWidget(self._lbl_filetype, 2, 1)
        self._lbl_filesize = QLabel("", self)
        self._lbl_filesize.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(self._lbl_filesize, 2, 2)

        grid.setColumnStretch(1, 1)
        root.addLayout(grid)

        # ---- action row -----------------------------------------------
        actions = QHBoxLayout()
        actions.setSpacing(6)
        self._btn_flash = QPushButton("Flash firmware", self)
        self._btn_flash.clicked.connect(self._on_flash_clicked)
        actions.addWidget(self._btn_flash)
        self._btn_abort = QPushButton("Abort", self)
        self._btn_abort.clicked.connect(self._on_abort_clicked)
        self._btn_abort.setEnabled(False)
        actions.addWidget(self._btn_abort)
        actions.addStretch(1)
        root.addLayout(actions)

        # ---- progress bar ---------------------------------------------
        self._progress = QProgressBar(self)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFormat("%p%")
        self._progress.setTextVisible(True)
        root.addWidget(self._progress)

        # ---- log view --------------------------------------------------
        root.addWidget(QLabel("Log:", self))
        self._log_view = QPlainTextEdit(self)
        self._log_view.setReadOnly(True)
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        self._log_view.setFont(mono)
        # Cap retained scrollback so a chatty stm32flash run doesn't blow the
        # heap on long flashes; QPlainTextEdit drops oldest blocks past this.
        self._log_view.setMaximumBlockCount(5000)
        root.addWidget(self._log_view, 1)

    # ------------------------------------------------------------------ radio wiring
    def _wire_radio(self) -> None:
        # Different host apps stub Radio with slightly different signal sets;
        # connect defensively so the smoke test (which uses a partial stub)
        # doesn't choke on a missing signal.
        for name, slot in (
            ("connected", self._on_connected),
            ("disconnected", self._on_disconnected),
            ("state_changed", self._on_state_changed),
            ("radio_info", self._on_radio_info),
            ("error", self._on_radio_error),
            ("log", self._on_radio_log),
        ):
            sig = getattr(self._radio, name, None)
            if sig is not None and hasattr(sig, "connect"):
                try:
                    sig.connect(slot)
                except Exception:
                    pass

    # ------------------------------------------------------------------ status helpers
    def _emit_status(self, text: str, timeout_ms: int = 5000) -> None:
        self.status_message.emit(text, timeout_ms)

    def _append_log(self, line: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        # Strip a single trailing newline if present; QPlainTextEdit adds its
        # own block break per appendPlainText call.
        line = line.rstrip("\r\n")
        self._log_view.appendPlainText(f"[{ts}] {line}")
        # Autoscroll: pin the cursor to the end so new lines stay visible.
        sb = self._log_view.verticalScrollBar()
        if sb is not None:
            sb.setValue(sb.maximum())

    # ------------------------------------------------------------------ slots: radio signals
    @Slot(str, int)
    def _on_connected(self, port: str, baud: int) -> None:
        self._port = port
        self._baud = int(baud)
        self._connected = True
        self._update_buttons()

    @Slot(str)
    def _on_disconnected(self, _reason: str) -> None:
        self._connected = False
        # Don't clear _port/_baud — we may need them to reopen after a flash.
        self._update_buttons()

    @Slot(str)
    def _on_state_changed(self, _state: str) -> None:
        # Buttons depend on connected-ness primarily, but state transitions
        # can re-enable Flash after the radio recovers from bootloader mode.
        self._update_buttons()

    @Slot(object)
    def _on_radio_info(self, info: object) -> None:
        if isinstance(info, dict):
            self._radio_info = info
            # Refresh _port/_baud in case the connected signal didn't fire
            # (e.g. the host app restored a previous session).
            p = getattr(self._radio, "_port", "") or self._port
            b = int(getattr(self._radio, "_baud", 0) or self._baud or 0)
            if p:
                self._port = p
            if b:
                self._baud = b
                self._connected = True
        self._update_detected_label()
        self._update_buttons()

    @Slot(str)
    def _on_radio_error(self, msg: str) -> None:
        # Surface radio-side errors in the firmware log so the user has the
        # full timeline in one place. Don't pop a dialog from here — the
        # Radio object surfaces those itself elsewhere in the app.
        self._append_log(f"radio error: {msg}")

    @Slot(str, int)
    def _on_radio_log(self, text: str, level: int) -> None:
        # Only mirror warnings/errors and "entered bootloader" — the data-mode
        # spam would drown out the flash log.
        if level >= 1 or "bootloader" in text.lower() or "update" in text.lower():
            self._append_log(text)

    # ------------------------------------------------------------------ slots: file picker
    @Slot()
    def _on_browse(self) -> None:
        start = os.path.dirname(self._path_edit.text()) or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select firmware file",
            start,
            "Firmware files (*.ihx *.hex *.bin);;Intel HEX (*.ihx *.hex);;Binary (*.bin);;All files (*)",
        )
        if path:
            self._path_edit.setText(path)

    @Slot(str)
    def _on_path_changed(self, _text: str) -> None:
        self._update_file_labels()
        self._update_buttons()

    # ------------------------------------------------------------------ display helpers
    def _update_detected_label(self) -> None:
        info = self._radio_info
        if not info:
            self._lbl_detected.setText("(connect to a radio first)")
            return

        bid = info.get("board_id")
        name = info.get("board_name") or board_name(bid if isinstance(bid, int) else None)
        freq_id = info.get("freq_id")

        # Map freq_id to a human label using the protocol module's table when
        # we can; fall back to the raw hex.
        from rfd.protocol import FREQ_IDS
        freq = FREQ_IDS.get(freq_id, f"0x{freq_id:02X}") if isinstance(freq_id, int) else "?"

        bid_str = f"0x{bid:02X}" if isinstance(bid, int) else "?"
        self._lbl_detected.setText(f"{name}  (board_id={bid_str}, freq={freq})")

    def _update_file_labels(self) -> None:
        path = self._path_edit.text().strip()
        if not path:
            self._lbl_filetype.setText("—")
            self._lbl_filesize.setText("")
            return
        self._lbl_filetype.setText(_file_type_label(path))
        if os.path.isfile(path):
            try:
                size = os.path.getsize(path)
                self._lbl_filesize.setText(f"Size: {_human_bytes(size)}")
            except OSError as e:
                self._lbl_filesize.setText(f"(stat failed: {e})")
        else:
            self._lbl_filesize.setText("(file not found)")

    def _update_buttons(self) -> None:
        flashing = self._thread is not None and self._thread.isRunning()
        path = self._path_edit.text().strip()
        kind = _classify(path)
        have_file = bool(path) and os.path.isfile(path) and kind != ""

        self._btn_flash.setEnabled(have_file and self._connected and not flashing)
        self._btn_abort.setEnabled(flashing)
        self._btn_browse.setEnabled(not flashing)
        self._path_edit.setEnabled(not flashing)

    # ------------------------------------------------------------------ slots: flash workflow
    @Slot()
    def _on_flash_clicked(self) -> None:
        path = self._path_edit.text().strip()
        if not path or not os.path.isfile(path):
            QMessageBox.warning(self, "Firmware", "Select a firmware file first.")
            return
        kind = _classify(path)
        if not kind:
            QMessageBox.warning(
                self,
                "Firmware",
                "Unrecognised file extension — choose .ihx, .hex, or .bin.",
            )
            return

        # Cross-check file kind against the detected board, if known.
        info = self._radio_info or {}
        board_id = info.get("board_id") if isinstance(info, dict) else None
        # Only cross-check boards we can classify: an unrecognised ID (e.g.
        # a newer x2/ux2 variant) must not be assumed to be 8051.
        if isinstance(board_id, int) and not board_name(board_id).startswith("Unknown"):
            board_kind = "stm32" if is_stm32_board(board_id) else "8051"
            if kind != board_kind:
                ans = QMessageBox.warning(
                    self,
                    "File / board mismatch",
                    (
                        f"File looks like {kind.upper()} ({_file_type_label(path)}) but "
                        f"radio is {board_kind.upper()} "
                        f"({info.get('board_name') or board_name(board_id)}).\n\n"
                        "Proceed anyway?"
                    ),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if ans != QMessageBox.StandardButton.Yes:
                    return

        # Final confirmation.
        ans = QMessageBox.question(
            self,
            "Flash firmware",
            "Flashing firmware will erase the radio. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return

        # Capture port/baud now — we'll need them to reopen the radio after.
        port = self._port or getattr(self._radio, "_port", "") or ""
        baud = self._baud or int(getattr(self._radio, "_baud", 0) or 0) or 57600
        if not port:
            QMessageBox.critical(
                self,
                "Firmware",
                "No serial port known. Connect to the radio first.",
            )
            return

        if kind == "stm32":
            self._start_stm32(path, port, baud, board_id if isinstance(board_id, int) else None)
        else:
            self._start_8051(path, port, baud, board_id if isinstance(board_id, int) else None)

    def _start_stm32(
        self,
        bin_path: str,
        port: str,
        baud: int,
        expected_board_id: int | None,
    ) -> None:
        # Pre-flight: stm32flash binary must be on PATH.
        from rfd.uploader_stm32 import stm32flash_available, stm32flash_install_hint

        if not stm32flash_available():
            QMessageBox.critical(
                self,
                "stm32flash not found",
                stm32flash_install_hint(),
            )
            return

        self._reset_run_state()
        self._flashing_kind = "stm32"
        self._append_log(f"flashing STM32 firmware: {bin_path}")
        self._append_log(f"port={port}, baud={baud}")

        # Drop into bootloader. The Radio object handles +++ then AT&UPDATE
        # and emits state_changed("bootloader") on success.
        self._append_log("requesting bootloader (AT&UPDATE)…")
        if not self._request_bootloader():
            return

        # stm32flash needs exclusive access to the port.  close_port is
        # queued on the radio's worker — wait until it has actually run.
        self._append_log("releasing serial port for stm32flash…")
        try:
            self._wait_future(self._radio.close_port(), timeout_ms=5000)
        except Exception as e:
            self._append_log(f"close_port: {e}")

        # Small settle — let the OS actually release the tty before
        # stm32flash tries to claim it.
        QApplication.processEvents()
        time.sleep(0.3)

        self._append_log("starting stm32flash…")
        self._spawn_worker(
            _FlashWorker(
                "stm32",
                port=port,
                bin_path=bin_path,
                baud=baud,
                expected_board_id=expected_board_id,
            )
        )

    def _start_8051(
        self,
        ihx_path: str,
        port: str,
        baud: int,
        expected_board_id: int | None,
    ) -> None:
        from rfd.ihx import IHXError, parse_file

        try:
            image = parse_file(ihx_path)
        except (IHXError, OSError) as e:
            QMessageBox.critical(self, "Firmware", f"Failed to parse {ihx_path}:\n{e}")
            return

        self._reset_run_state()
        self._flashing_kind = "8051"
        self._append_log(f"flashing 8051 firmware: {ihx_path}")
        self._append_log(
            f"image: {len(image.chunks)} chunk(s), {image.total_bytes()} payload bytes"
        )
        self._append_log(f"port={port}, baud={baud}")
        self._append_log("requesting bootloader (AT&UPDATE)…")
        if not self._request_bootloader():
            return

        # The 8051 bootloader speaks the SiK custom protocol on the same
        # serial handle. Grab the underlying pyserial object via _core.
        core = getattr(self._radio, "_core", None)
        ser = getattr(core, "serial", None) if core is not None else None
        if ser is None:
            self._flashing_kind = ""
            QMessageBox.critical(
                self,
                "Firmware",
                "Could not access the radio's serial port. Reconnect and try again.",
            )
            return

        self._spawn_worker(
            _FlashWorker(
                "8051",
                ser=ser,
                image=image,
                expected_board_id=expected_board_id,
            )
        )

    # ------------------------------------------------------------------ worker plumbing
    def _spawn_worker(self, worker: _FlashWorker) -> None:
        thread = QThread(self)
        worker.moveToThread(thread)
        # started -> run kicks off the flash on the worker thread.
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_worker_progress)
        worker.log.connect(self._on_worker_log)
        worker.finished.connect(self._on_worker_finished)
        # Standard cleanup chain: when the worker finishes, ask the thread
        # to quit, then schedule both for deletion. Using deleteLater rather
        # than direct ``del`` lets pending queued signals drain first.
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._worker = worker
        self._thread = thread
        thread.start()
        self._update_buttons()

    @Slot()
    def _on_abort_clicked(self) -> None:
        if self._worker is None:
            return
        ans = QMessageBox.question(
            self,
            "Abort flash",
            "Aborting mid-flash will leave the radio in an unknown state and "
            "you will likely need to re-flash before it works again.\n\nAbort anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._append_log("abort requested — waiting for worker to stop…")
        try:
            self._worker.cancel()
        except Exception as e:
            self._append_log(f"cancel: {e}")

    # ------------------------------------------------------------------ worker -> UI
    @Slot(int, int)
    def _on_worker_progress(self, done: int, total: int) -> None:
        if total > 0:
            pct = max(0, min(100, int(done * 100 / total)))
        else:
            pct = 0
        self._progress.setValue(pct)
        # KB-scale label embedded in the bar's format keeps the spec's
        # "146 KB / 234 KB" hint visible without needing a separate label.
        self._progress.setFormat(
            f"{pct}%  ({done // 1024} KB / {max(total, 1) // 1024} KB)"
        )

    @Slot(str)
    def _on_worker_log(self, line: str) -> None:
        self._append_log(line)

    @Slot(bool, str)
    def _on_worker_finished(self, ok: bool, msg: str) -> None:
        kind = self._flashing_kind
        self._append_log(f"worker finished: ok={ok}, msg={msg}")
        # Drop our references so a new flash can proceed cleanly. The thread
        # will deleteLater itself once its event loop unwinds.
        self._worker = None
        self._thread = None
        self._flashing_kind = ""

        if ok:
            self._emit_status("firmware updated", 6000)
            self._progress.setValue(100)
            self._progress.setFormat("100%  done")
            self._append_log("firmware update complete")
        else:
            # ``UploadCancelled`` / ``STM32FlashCancelled`` arrive here too —
            # treat them as a graceful abort rather than an error dialog.
            if "Cancelled" in msg or "cancelled" in msg.lower():
                self._append_log("flash aborted by user")
                self._emit_status("flash aborted", 5000)
            else:
                QMessageBox.critical(self, "Flash failed", msg)
                self._emit_status("flash failed", 6000)

        # Reopen the port. For 8051 the uploader sends REBOOT, so the radio
        # comes back on its own; we just need to wait briefly. For STM32 the
        # subprocess held the port — we can reopen as soon as it exits.
        if self._port and self._baud:
            # Slight delay so the radio finishes booting before we try to
            # +++ into command mode.
            QTimer.singleShot(
                1500 if kind == "8051" else 800,
                lambda: self._reopen_port(),
            )
        self._update_buttons()

    # ------------------------------------------------------------------ helpers
    def _reset_run_state(self) -> None:
        self._progress.setValue(0)
        self._progress.setFormat("%p%")
        self._log_view.clear()

    def _wait_future(self, future, *, timeout_ms: int) -> bool:
        """Keep the UI responsive until a queued Radio operation finishes."""
        deadline = time.monotonic() + timeout_ms / 1000.0
        while future is not None and not future.done() and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.02)
        return future is None or future.done()

    def _request_bootloader(self) -> bool:
        """Send AT&UPDATE and wait until the radio is really in its
        bootloader.  Entering command mode alone takes ≥2.2 s of +++ guard
        time; starting the uploader before that finishes would trample
        the escape sequence on the shared serial port."""
        try:
            future = self._radio.enter_bootloader()
            self._wait_future(future, timeout_ms=15000)
        except Exception as e:
            self._append_log(f"enter_bootloader failed: {e}")
            future = None
        # Let the queued state_changed("bootloader") reach this thread.
        if self._wait_for_state(Radio.STATE_BOOTLOADER, timeout_ms=1000):
            return True
        # No AT reply.  That's expected when recovering a radio held in its
        # bootloader by the BOOT pin, so offer to carry on rather than abort.
        ans = QMessageBox.question(
            self, "Firmware",
            "The radio did not confirm entering its bootloader (AT&UPDATE "
            "got no reply).\n\nIf you are recovering a radio by holding its "
            "BOOT pin, it is already in the bootloader — continue flashing?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self._append_log("no bootloader confirmation — continuing (BOOT-pin recovery)")
            self._radio.assume_bootloader()
            return True
        self._flashing_kind = ""
        self._append_log("radio did not enter the bootloader — aborted, nothing flashed")
        return False

    def _wait_for_state(self, target: str, *, timeout_ms: int) -> bool:
        """Spin the event loop briefly waiting for the radio to reach ``target``.

        We don't have a Qt waitable here — the Radio runs on a worker thread
        and emits ``state_changed`` via queued connections — so we just let
        the local event loop drain and check ``radio._state``. Returns True
        on hit, False on timeout.
        """
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            current = getattr(self._radio, "_state", None)
            if current == target:
                return True
            QApplication.processEvents()
            time.sleep(0.02)
        return False

    def _reopen_port(self) -> None:
        if not self._port or not self._baud:
            return
        self._append_log(f"reopening port {self._port} @ {self._baud}…")
        try:
            self._radio.open_port(self._port, self._baud)
        except Exception as e:
            self._append_log(f"open_port: {e}")


__all__ = ["FirmwareTab"]


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import sys

    app = QApplication.instance() or QApplication(sys.argv)
    radio = Radio()
    w = FirmwareTab(radio)
    w.resize(900, 600)
    w.show()
    sys.exit(app.exec())

"""Terminal tab — interactive AT/passthrough console for the connected radio.

Wraps a read-only :class:`QTextEdit` log area with an input box, command
history (up/down arrows), a "Hex" toggle for raw byte input, a "Command Mode"
toggle that switches between :meth:`Radio.send_raw_at` and direct serial
writes, and a "Clear" button.  Output is timestamped and lightly colourised:
TX lines are blue, RX lines black, hex dumps dim grey.

The tab disables all interactive widgets while the radio is disconnected.
Auto-scroll is suppressed when the user has scrolled up.
"""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QKeyEvent, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from rfd.radio import Radio


# ---------- helpers ---------------------------------------------------------
def _now_stamp() -> str:
    """Return an ``HH:MM:SS.mmm`` timestamp for log lines."""
    t = time.time()
    lt = time.localtime(t)
    ms = int((t - int(t)) * 1000)
    return f"{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}.{ms:03d}"


def _html_escape(s: str) -> str:
    """Minimal HTML escape so user input can't inject markup into the log."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _parse_hex(text: str) -> bytes:
    """Parse a string of space-separated hex bytes into ``bytes``.

    Tolerates extra whitespace and 0x prefixes.  Raises :class:`ValueError`
    on any malformed token.
    """
    out = bytearray()
    for tok in text.replace(",", " ").split():
        if tok.lower().startswith("0x"):
            tok = tok[2:]
        if not tok:
            continue
        if len(tok) % 2 != 0:
            tok = "0" + tok
        out.extend(bytes.fromhex(tok))
    return bytes(out)


def _hex_dump(data: bytes) -> str:
    """Format bytes as space-separated upper-case hex pairs."""
    return " ".join(f"{b:02X}" for b in data)


# ---------- input line edit with history -----------------------------------
class _HistoryLineEdit(QLineEdit):
    """:class:`QLineEdit` that cycles through a session-local history list
    when the up/down arrow keys are pressed.

    The history list is owned by the parent tab and shared across the
    widget's lifetime; this subclass only navigates it.
    """

    def __init__(self, history: list[str], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._history = history
        self._index: int = -1  # -1 == "past the end" (current draft line)
        self._draft: str = ""

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt API)
        key = event.key()
        if key == Qt.Key.Key_Up:
            self._navigate(-1)
            event.accept()
            return
        if key == Qt.Key.Key_Down:
            self._navigate(+1)
            event.accept()
            return
        # Any other keypress invalidates our position in history.
        self._index = -1
        super().keyPressEvent(event)

    def _navigate(self, direction: int) -> None:
        if not self._history:
            return
        if self._index == -1:
            # Save whatever the user was typing so down-arrow can restore it.
            self._draft = self.text()
            if direction < 0:
                self._index = len(self._history) - 1
            else:
                return  # nothing to recall going forward from the draft
        else:
            new_idx = self._index + direction
            if new_idx < 0:
                new_idx = 0
            if new_idx >= len(self._history):
                # Past the newest entry — restore the draft and detach.
                self._index = -1
                self.setText(self._draft)
                return
            self._index = new_idx
        self.setText(self._history[self._index])
        self.setCursorPosition(len(self.text()))


# ---------- the tab itself --------------------------------------------------
class TerminalTab(QWidget):
    """AT-terminal / passthrough console for an :class:`rfd.radio.Radio`."""

    # Colour palette for log lines.
    _COLOR_TX = "#1F4FB6"        # blue
    _COLOR_RX = "#202020"        # near-black
    _COLOR_DIM = "#888888"       # dim grey for hex dump
    _COLOR_NOTE = "#888888"      # state-change / info notes
    _COLOR_ERR = "#C0392B"       # red for errors

    def __init__(self, radio: Radio, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._radio = radio
        self._history: list[str] = []
        self._command_mode_active = False
        self._suppressed_scroll = False
        self._radio_state = Radio.STATE_DISCONNECTED

        # ---- log area ----------------------------------------------------
        self._log = QTextEdit(self)
        self._log.setReadOnly(True)
        self._log.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self._log.setUndoRedoEnabled(False)
        self._log.setAcceptRichText(True)
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        mono.setPointSize(10)
        self._log.setFont(mono)
        self._log.document().setDefaultStyleSheet(
            "p { margin: 0; white-space: pre-wrap; }"
        )

        # ---- input row ---------------------------------------------------
        self._input = _HistoryLineEdit(self._history, self)
        self._input.setFont(mono)
        self._input.setPlaceholderText("type AT command and press Enter…")

        self._send_btn = QPushButton("Send", self)

        self._hex_btn = QToolButton(self)
        self._hex_btn.setText("Hex")
        self._hex_btn.setCheckable(True)
        self._hex_btn.setToolTip(
            "When ON, the input is parsed as space-separated hex bytes."
        )

        self._cmd_btn = QToolButton(self)
        self._cmd_btn.setText("Command Mode")
        self._cmd_btn.setCheckable(True)
        self._cmd_btn.setToolTip(
            "When ON, sends are routed through Radio.send_raw_at() and the "
            "radio is held in command mode.  When OFF, sends are written "
            "straight to the serial port (passthrough)."
        )

        self._clear_btn = QPushButton("Clear", self)

        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.addWidget(self._input, 1)
        input_row.addWidget(self._send_btn)
        input_row.addWidget(self._hex_btn)
        input_row.addWidget(self._cmd_btn)
        input_row.addWidget(self._clear_btn)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.addWidget(self._log, 1)
        outer.addLayout(input_row)

        # ---- internal wiring --------------------------------------------
        self._send_btn.clicked.connect(self._on_send)
        self._input.returnPressed.connect(self._on_send)
        self._clear_btn.clicked.connect(self._log.clear)
        self._cmd_btn.toggled.connect(self._on_cmd_mode_toggled)

        # ---- radio wiring -----------------------------------------------
        radio.rx_data.connect(self._on_rx_data)
        radio.at_response.connect(self._on_at_response)
        radio.error.connect(self._on_error)
        radio.state_changed.connect(self._on_state_changed)
        radio.log.connect(self._on_log)

        # Initial enable state — assume disconnected until told otherwise.
        self._set_widgets_enabled(False)

    # ---------------------------------------------------------- send path
    def _on_send(self) -> None:
        text = self._input.text()
        if not text:
            return

        hex_mode = self._hex_btn.isChecked()
        cmd_mode = self._cmd_btn.isChecked()

        if hex_mode:
            try:
                payload = _parse_hex(text)
            except ValueError as e:
                self._append_note(f"hex parse error: {e}", self._COLOR_ERR)
                return
            if not payload:
                self._append_note("hex parse error: no bytes", self._COLOR_ERR)
                return
            if cmd_mode:
                self._append_note(
                    "hex sends only work in passthrough (Command Mode is ON)",
                    self._COLOR_ERR,
                )
                return
            ser = self._serial_or_none()
            if ser is None:
                self._append_note(
                    "no serial available for hex passthrough",
                    self._COLOR_ERR,
                )
                return
            try:
                ser.write(payload)
                try:
                    ser.flush()
                except Exception:
                    pass
            except Exception as e:
                self._append_note(f"serial write failed: {e}", self._COLOR_ERR)
                return
            # Display: ASCII rendering on TX line, hex dump under it.
            ascii_repr = payload.decode("ascii", errors="replace")
            self._append_tx(ascii_repr)
            self._append_dim("  " + _hex_dump(payload))
        else:
            if cmd_mode:
                self._command_mode_active = True
                self._append_tx(text)
                self._radio.send_raw_at(text)
            else:
                ser = self._serial_or_none()
                if ser is None:
                    self._append_note(
                        "no serial available for passthrough write",
                        self._COLOR_ERR,
                    )
                    return
                try:
                    ser.write((text + "\r\n").encode("ascii", errors="replace"))
                    try:
                        ser.flush()
                    except Exception:
                        pass
                except Exception as e:
                    self._append_note(f"serial write failed: {e}", self._COLOR_ERR)
                    return
                self._append_tx(text)

        # Update history and clear the input.
        if not self._history or self._history[-1] != text:
            self._history.append(text)
        self._input.clear()

    def _on_cmd_mode_toggled(self, checked: bool) -> None:
        if checked:
            # Lazily probe with AT to coax the radio into command mode.
            self._command_mode_active = True
            try:
                self._radio.send_raw_at("AT")
            except Exception as e:
                self._append_note(f"send AT failed: {e}", self._COLOR_ERR)
        else:
            self._command_mode_active = False
            try:
                self._radio.back_to_data()
            except Exception as e:
                self._append_note(f"back_to_data failed: {e}", self._COLOR_ERR)

    # ---------------------------------------------------------- visibility
    def showEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().showEvent(event)
        # Other tabs (an RSSI session, Load Settings…) may have taken the
        # radio out of command mode.  If Command Mode is on, re-assert it so
        # the next typed command works first time; the OK shows it's live.
        if (
            self._cmd_btn.isChecked()
            and self._radio_state in (Radio.STATE_DATA, Radio.STATE_COMMAND)
        ):
            self._radio.send_raw_at("AT")

    # ---------------------------------------------------------- radio signals
    def _on_rx_data(self, data: bytes) -> None:
        if not data:
            return
        try:
            text = data.decode("ascii", errors="replace")
        except Exception:
            text = repr(data)
        self._append_rx(text, prefix="")
        if self._hex_btn.isChecked():
            self._append_dim("  " + _hex_dump(data))

    def _on_at_response(self, _cmd: str, response: str, ok: bool) -> None:
        # Strip trailing whitespace so multi-line replies render cleanly.
        text = response.rstrip()
        if not text:
            return
        if ok:
            self._append_rx(text, prefix="← ")  # leftwards arrow
        else:
            self._append_note(f"← {text}", self._COLOR_ERR)

    def _on_error(self, msg: str) -> None:
        self._append_note(f"! {msg}", self._COLOR_ERR)

    def _on_log(self, text: str, level: int) -> None:
        if level >= 2:
            colour = self._COLOR_ERR
        elif level == 1:
            colour = "#B7791F"
        else:
            colour = self._COLOR_NOTE
        self._append_note(text, colour)

    def _on_state_changed(self, state: str) -> None:
        self._radio_state = state
        connected = state != Radio.STATE_DISCONNECTED
        self._set_widgets_enabled(connected)
        self._append_note(f"[state: {state}]", self._COLOR_NOTE)

    # ---------------------------------------------------------- helpers
    def _serial_or_none(self):
        """Return the underlying pyserial-ish object, or None if unavailable.

        Reaches through ``radio._core.serial`` (the public surface used by
        :class:`RadioCore`); falls back to a couple of getattr probes for
        forward-compatibility.
        """
        for attr in ("_core", "core"):
            core = getattr(self._radio, attr, None)
            if core is None:
                continue
            ser = getattr(core, "serial", None)
            if ser is not None:
                return ser
        return None

    def _set_widgets_enabled(self, enabled: bool) -> None:
        self._input.setEnabled(enabled)
        self._send_btn.setEnabled(enabled)
        self._hex_btn.setEnabled(enabled)
        self._cmd_btn.setEnabled(enabled)
        # Clear button stays usable so users can tidy the log even when
        # disconnected.

    # ---------------------------------------------------------- log painting
    def _user_at_bottom(self) -> bool:
        """Whether the log's vertical scrollbar is parked at the bottom.

        Used to decide whether to auto-scroll on append; we leave the user's
        view alone if they've scrolled up to read history.
        """
        bar = self._log.verticalScrollBar()
        if bar is None:
            return True
        # A small slack lets a freshly-appended line still count as "at bottom".
        return bar.value() >= bar.maximum() - 2

    def _append_html(self, html: str) -> None:
        at_bottom = self._user_at_bottom()
        cursor = self._log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertHtml(html)
        cursor.insertBlock()
        if at_bottom:
            bar = self._log.verticalScrollBar()
            if bar is not None:
                bar.setValue(bar.maximum())

    def _append_tx(self, text: str) -> None:
        stamp = _now_stamp()
        body = _html_escape(text)
        html = (
            f'<span style="color:{self._COLOR_DIM};">[{stamp}]</span> '
            f'<span style="color:{self._COLOR_TX};">&raquo; {body}</span>'
        )
        self._append_html(html)

    def _append_rx(self, text: str, *, prefix: str = "") -> None:
        stamp = _now_stamp()
        # Replies often include CR/LF + an "OK" prompt — render each line.
        for line in text.replace("\r", "").splitlines() or [text]:
            if not line:
                continue
            body = _html_escape(prefix + line)
            html = (
                f'<span style="color:{self._COLOR_DIM};">[{stamp}]</span> '
                f'<span style="color:{self._COLOR_RX};">{body}</span>'
            )
            self._append_html(html)

    def _append_dim(self, text: str) -> None:
        body = _html_escape(text)
        html = f'<span style="color:{self._COLOR_DIM};">{body}</span>'
        self._append_html(html)

    def _append_note(self, text: str, colour: str) -> None:
        stamp = _now_stamp()
        body = _html_escape(text)
        html = (
            f'<span style="color:{self._COLOR_DIM};">[{stamp}]</span> '
            f'<span style="color:{colour};">{body}</span>'
        )
        self._append_html(html)

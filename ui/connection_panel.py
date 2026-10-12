"""Header connection panel: port/baud selection plus connect lifecycle.

Owns the visible connection UI (port dropdown, baud dropdown, refresh button,
Connect/Disconnect button, status LED + label) and wires itself to a supplied
:class:`rfd.radio.Radio` instance.  The panel re-emits ``connect_requested``
and ``disconnect_requested`` so the surrounding window can update a status bar
without having to re-subscribe to the radio's signals.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from rfd.detector import list_serial_ports
from rfd.radio import Radio

from .theme import STATUS_LED_QSS


# Standard SiK baud rates plus MultiComm's higher S1BAUD options.  57600 is
# the factory default for both RFD900x and MultiComm.
_BAUDS: tuple[int, ...] = (
    2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400,
    460800, 1000000, 1200000, 1500000,
)
_DEFAULT_BAUD = 57600


class ConnectionPanel(QWidget):
    """Header panel that owns connection lifecycle UI.

    Construct with a :class:`Radio` instance; the panel wires itself to the
    Radio's signals and slots.
    """

    # Re-emitted for main_window's convenience (status bar, etc.)
    connect_requested = Signal(str, int)   # port, baud
    disconnect_requested = Signal()

    def __init__(self, radio: Radio, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._radio = radio
        self._state = Radio.STATE_DISCONNECTED
        self._port = ""
        self._baud = 0
        self._board_name = ""

        # ---- widgets -------------------------------------------------
        self._port_combo = QComboBox(self)
        self._port_combo.setMinimumWidth(320)
        self._port_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._port_model = QStandardItemModel(self._port_combo)
        self._port_combo.setModel(self._port_model)

        self._baud_combo = QComboBox(self)
        for b in _BAUDS:
            self._baud_combo.addItem(str(b), b)
        self._baud_combo.setCurrentIndex(_BAUDS.index(_DEFAULT_BAUD))

        self._refresh_btn = QToolButton(self)
        self._refresh_btn.setText("Refresh")
        self._refresh_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

        self._connect_btn = QPushButton("Connect", self)
        self._connect_btn.setMinimumWidth(110)

        self._led = QFrame(self)
        self._led.setFrameShape(QFrame.Shape.NoFrame)
        self._led.setStyleSheet(STATUS_LED_QSS["off"])
        self._led.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self._status_label = QLabel("Disconnected", self)
        self._status_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )

        # ---- layout --------------------------------------------------
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Port:", self))
        row.addWidget(self._port_combo, 1)
        row.addWidget(self._refresh_btn)
        row.addSpacing(8)
        row.addWidget(QLabel("Baud:", self))
        row.addWidget(self._baud_combo)
        row.addSpacing(8)
        row.addWidget(self._connect_btn)
        row.addSpacing(12)
        row.addWidget(self._led)
        row.addWidget(self._status_label, 1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.addLayout(row)

        # ---- error LED flash timer ----------------------------------
        # Single-shot so repeated errors keep the LED red until things settle.
        self._err_flash_timer = QTimer(self)
        self._err_flash_timer.setSingleShot(True)
        self._err_flash_timer.setInterval(800)
        self._err_flash_timer.timeout.connect(self._restore_led_for_state)

        # ---- internal wiring ----------------------------------------
        self._refresh_btn.clicked.connect(self.refresh_ports)
        self._connect_btn.clicked.connect(self._on_connect_clicked)

        # ---- radio wiring -------------------------------------------
        radio.state_changed.connect(self._on_state_changed)
        radio.connected.connect(self._on_connected)
        radio.disconnected.connect(self._on_disconnected)
        radio.radio_info.connect(self._on_radio_info)
        radio.error.connect(self._on_error)

        # Populate ports once everything is wired.
        self.refresh_ports()

    # ----------------------------------------------------------------- public
    def refresh_ports(self) -> None:
        """Repopulate the port dropdown via :func:`rfd.detector.list_serial_ports`.

        Likely-radio entries are listed first (the detector already sorts) and
        rendered bold.  The previously-selected device is reselected if it is
        still present.
        """
        previous = self.selected_port()

        self._port_model.clear()
        ports = list_serial_ports()

        if not ports:
            item = QStandardItem("(no serial ports detected)")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._port_model.appendRow(item)
            return

        bold = QFont()
        bold.setBold(True)

        restore_idx = -1
        for i, p in enumerate(ports):
            usb = p.usb_label()
            label = f"{p.device} — {p.description}"
            if usb:
                label += f" [{usb}]"
            item = QStandardItem(label)
            item.setData(p.device, Qt.ItemDataRole.UserRole)
            if p.likely_radio:
                item.setData(bold, Qt.ItemDataRole.FontRole)
            self._port_model.appendRow(item)
            if previous and p.device == previous:
                restore_idx = i

        if restore_idx >= 0:
            self._port_combo.setCurrentIndex(restore_idx)
        else:
            self._port_combo.setCurrentIndex(0)

    def selected_port(self) -> str | None:
        idx = self._port_combo.currentIndex()
        if idx < 0:
            return None
        item = self._port_model.item(idx)
        if item is None or not (item.flags() & Qt.ItemFlag.ItemIsEnabled):
            return None
        device = item.data(Qt.ItemDataRole.UserRole)
        return device if isinstance(device, str) and device else None

    def selected_baud(self) -> int:
        data = self._baud_combo.currentData()
        return int(data) if data is not None else _DEFAULT_BAUD

    # --------------------------------------------------------------- internal
    def _on_connect_clicked(self) -> None:
        if self._state == Radio.STATE_DISCONNECTED:
            port = self.selected_port()
            if not port:
                self._status_label.setText("No serial port selected")
                return
            baud = self.selected_baud()
            self.connect_requested.emit(port, baud)
            self._radio.open_port(port, baud)
        else:
            self.disconnect_requested.emit()
            self._radio.close_port()

    # ---- radio signal handlers --------------------------------------
    def _on_state_changed(self, state: str) -> None:
        self._state = state
        self._update_connect_button()
        # Don't clobber an active error flash.
        if not self._err_flash_timer.isActive():
            self._apply_led_for_state()

    def _on_connected(self, port: str, baud: int) -> None:
        self._port = port
        self._baud = baud
        # Board name may arrive separately via radio_info; show what we have.
        if self._board_name:
            self._status_label.setText(
                f"Connected to {port} @ {baud} — {self._board_name}"
            )
        else:
            self._status_label.setText(f"Connected to {port} @ {baud}")
        self._update_connect_button()

    def _on_disconnected(self, reason: str) -> None:
        self._port = ""
        self._baud = 0
        self._board_name = ""
        text = "Disconnected"
        if reason:
            text = f"Disconnected ({reason})"
        self._status_label.setText(text)
        self._update_connect_button()

    def _on_radio_info(self, info: object) -> None:
        board_name = ""
        if isinstance(info, dict):
            board_name = str(info.get("board_name") or "")
            if info.get("firmware_family") == "multicomm":
                board_name = f"{board_name} · MultiComm" if board_name else "MultiComm"
        self._board_name = board_name
        if self._port and self._baud:
            if board_name:
                self._status_label.setText(
                    f"Connected to {self._port} @ {self._baud} — {board_name}"
                )
            else:
                self._status_label.setText(
                    f"Connected to {self._port} @ {self._baud}"
                )

    def _on_error(self, _msg: str) -> None:
        self._led.setStyleSheet(STATUS_LED_QSS["err"])
        self._err_flash_timer.start()

    # ---- LED + button helpers --------------------------------------
    def _led_key_for_state(self) -> str:
        if self._state in (Radio.STATE_DATA, Radio.STATE_COMMAND):
            return "ok"
        if self._state == Radio.STATE_BOOTLOADER:
            return "warn"
        return "off"

    def _apply_led_for_state(self) -> None:
        self._led.setStyleSheet(STATUS_LED_QSS[self._led_key_for_state()])

    def _restore_led_for_state(self) -> None:
        self._apply_led_for_state()

    def _update_connect_button(self) -> None:
        if self._state == Radio.STATE_DISCONNECTED:
            self._connect_btn.setText("Connect")
        else:
            self._connect_btn.setText("Disconnect")

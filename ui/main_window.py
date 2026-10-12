"""Main application window: assembles the connection panel + tabs and wires
status messages, errors, and the global log into the status bar."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from rfd.radio import Radio

from .connection_panel import ConnectionPanel
from .firmware_tab import FirmwareTab
from .rssi_tab import RssiTab
from .settings_tab import SettingsTab
from .system_checks import SystemIssue, run_all_checks
from .terminal_tab import TerminalTab


APP_TITLE = "rfdtool — RFD900 configuration"


class MainWindow(QMainWindow):
    def __init__(self, radio: Radio | None = None) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1100, 760)

        self.radio = radio if radio is not None else Radio()

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 4)
        layout.setSpacing(6)

        self.connection_panel = ConnectionPanel(self.radio, parent=central)
        layout.addWidget(self.connection_panel)

        self.tabs = QTabWidget(central)
        self.settings_tab = SettingsTab(self.radio)
        self.terminal_tab = TerminalTab(self.radio)
        self.rssi_tab = RssiTab(self.radio)
        self.firmware_tab = FirmwareTab(self.radio)
        self.tabs.addTab(self.settings_tab, "Settings")
        self.tabs.addTab(self.terminal_tab, "Terminal")
        self.tabs.addTab(self.rssi_tab, "RSSI / Link")
        self.tabs.addTab(self.firmware_tab, "Firmware")
        layout.addWidget(self.tabs, 1)

        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar(self))
        self.statusBar().showMessage("Disconnected")

        self._build_menus()
        self._wire_status_signals()

    # ------------------------------------------------------------------ menus
    def _build_menus(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = bar.addMenu("&Help")

        wizard_action = QAction("&Pairing wizard…", self)
        wizard_action.setShortcut("Ctrl+P")
        wizard_action.triggered.connect(self._show_pairing_wizard)
        help_menu.addAction(wizard_action)

        guide_action = QAction("Pairing &guide", self)
        guide_action.triggered.connect(self._show_pairing_guide)
        help_menu.addAction(guide_action)

        help_menu.addSeparator()

        about_action = QAction("&About rfdtool", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _show_pairing_wizard(self) -> None:
        from .pairing_wizard import PairingWizard
        st = self.settings_tab
        # Capture current panel mappings via providers so the wizard always
        # sees fresh values even after the panel rebuilds on a fresh ATI5.
        wizard = PairingWizard(
            self.radio,
            name_to_sreg_provider=lambda: st._local_panel.name_to_sreg(),
            sreg_to_name_provider=lambda: st._local_panel.sreg_to_name(),
            firmware_banner_provider=lambda: st._firmware_banner,
            board_name_provider=lambda: st._board_name,
            parent=self,
        )
        # Pipe Stage-5 / Compare's "Apply fix" signals back to the settings
        # tab's existing fix-staging plumbing.
        wizard.apply_fix_requested.connect(st._on_apply_validation_fix)
        wizard.exec()
        # Parented to the main window, so without this every closed wizard
        # (and its radio signal connections) would live until exit.
        wizard.deleteLater()

    def _show_pairing_guide(self) -> None:
        from .pairing_guide import PairingGuideDialog
        guide = PairingGuideDialog(parent=self)
        guide.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        guide.open_wizard_requested.connect(self._show_pairing_wizard)
        guide.show()

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "About rfdtool",
            "<b>rfdtool</b><br>"
            "Linux GUI for configuring RFD900-series radio modems running "
            "SiK or RFD MultiComm (v4.x) firmware."
            "<br><br>"
            "Developer: <b>Skoll</b><br>"
            'Website: <a href="https://skoll.dev">skoll.dev</a><br>'
            'Support: <a href="mailto:me@skoll.dev">me@skoll.dev</a><br>'
            'Tool family: <a href="https://TheIT.guru/sUASTools">TheIT.guru/sUASTools</a>'
            "<br><br>"
            "MIT licensed.",
        )

    # ------------------------------------------------------------------ wiring
    def _wire_status_signals(self) -> None:
        # Each tab can emit a transient status_message; we route them all to
        # the status bar.  Tabs without that signal are skipped silently.
        for tab in (self.settings_tab, self.firmware_tab):
            sig = getattr(tab, "status_message", None)
            if sig is not None and hasattr(sig, "connect"):
                sig.connect(lambda msg, ms: self.statusBar().showMessage(msg, ms))

        self.radio.error.connect(self._on_radio_error)
        self.radio.log.connect(self._on_radio_log)
        self.radio.state_changed.connect(self._on_state_changed)
        self.radio.connected.connect(self._on_connected)
        self.radio.disconnected.connect(self._on_disconnected)

    def _on_radio_error(self, msg: str) -> None:
        self.statusBar().showMessage(f"⚠ {msg}", 8000)

    def _on_radio_log(self, msg: str, level: int) -> None:
        prefix = ("", "warn: ", "error: ")[max(0, min(2, level))]
        self.statusBar().showMessage(f"{prefix}{msg}", 5000)

    def _on_state_changed(self, state: str) -> None:
        self.statusBar().showMessage(f"State: {state}", 3000)

    def _on_connected(self, port: str, baud: int) -> None:
        self.statusBar().showMessage(f"Connected to {port} @ {baud}", 5000)

    def _on_disconnected(self, reason: str) -> None:
        self.statusBar().showMessage(f"Disconnected: {reason}", 5000)

    # ------------------------------------------------------------------ startup checks
    def show_system_issues(self, issues: list[SystemIssue]) -> None:
        """Show one combined dialog if any issues were detected."""
        if not issues:
            return
        worst = max(
            issues, key=lambda i: ("info", "warn", "error").index(i.severity)
        ).severity
        title = "System checks"
        body_parts: list[str] = []
        for i in issues:
            tag = {"info": "ⓘ", "warn": "⚠", "error": "✕"}[i.severity]
            body_parts.append(f"{tag} <b>{i.title}</b><br><pre>{i.detail}</pre>")
        body = "<br>".join(body_parts)
        if worst == "error":
            QMessageBox.critical(self, title, body)
        elif worst == "warn":
            QMessageBox.warning(self, title, body)
        else:
            QMessageBox.information(self, title, body)

    # ------------------------------------------------------------------ shutdown
    def closeEvent(self, event) -> None:  # type: ignore[override]
        try:
            self.radio.shutdown()
        except Exception:
            pass
        super().closeEvent(event)


def run_startup_checks() -> list[SystemIssue]:
    return run_all_checks()

"""Pairing wizard — five-stage modal that walks a user through binding
two radios.

Stage 1: Welcome / pick mode (one PC + swap, vs. cross-PC with file transfer).
Stage 2: Configure radio A (preset → Apply & Persist).
Stage 3: Save reference profile (live ATI5 → JSON for transfer).
Stage 4: Configure radio B.
            Same-PC: prompt to physically swap, detect new connection, re-apply.
            Cross-PC: manual instructions; user confirms when done on the
                      other side.
Stage 5: Verify link (live RSSI + packet count; Compare-with-profile on fail).
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from rfd.presets import (
    BUILT_IN_PRESETS,
    Profile,
    load_profile,
    profile_from_ati5,
    save_profile,
    save_user_profile,
)
from .apply_persist_dialog import ApplyPersistDialog
from .compare_dialog import CompareConfigsDialog


_OK_GREEN = "#27ae60"
_WARN_AMBER = "#d68910"
_ERR_RED = "#c0392b"


class _Stage(QWidget):
    """Base class — signals when the user has completed this stage."""
    complete = Signal()

    def reset(self) -> None:
        """Called by the wizard when navigating back to this stage."""

    def is_complete(self) -> bool:
        return False


class _WelcomeStage(_Stage):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "<h3>Welcome to the radio pairing wizard.</h3>"
            "<p>I'll walk you through configuring two radios with identical "
            "settings, saving them to EEPROM, rebooting both, and verifying "
            "they linked. Takes ~2 minutes per radio.</p>"
            "<p><b>MultiComm radios:</b> the default automatic-master mode "
            "pairs with identical settings. For fixed-master mode use the "
            "'fixed master: master' preset on radio A and 'fixed master: "
            "node' on radio B — they must have different NODEID/SLOTIDS.</p>"
            "<p>How are your radios connected?</p>"
        )
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._mode_group = QButtonGroup(self)
        self._same_pc = QRadioButton(
            "I have one PC and will physically swap the USB cable between radios"
        )
        self._cross_pc = QRadioButton(
            "Each radio is on its own PC (I'll transfer a JSON profile file)"
        )
        self._same_pc.setChecked(True)
        self._mode_group.addButton(self._same_pc, 0)
        self._mode_group.addButton(self._cross_pc, 1)
        layout.addWidget(self._same_pc)
        layout.addWidget(self._cross_pc)

        layout.addStretch(1)

    def mode(self) -> str:
        return "same_pc" if self._same_pc.isChecked() else "cross_pc"

    def is_complete(self) -> bool:
        return True


class _ConfigureStage(_Stage):
    """Stage 2 / part of Stage 4 — pick a source config and apply it."""

    applied = Signal(object)   # ApplyPersistReport

    def __init__(
        self,
        radio,
        title: str,
        *,
        allow_use_panel: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._radio = radio
        self._title = title
        self._chosen_profile: Profile | None = None
        self._last_report = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<h3>{title}</h3>"))

        self._radio_label = QLabel("Detecting connected radio…")
        self._radio_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._radio_label)

        # Source picker
        source_group = QGroupBox("Where should the config come from?")
        source_layout = QVBoxLayout(source_group)
        self._source_combo = QComboBox()
        # Built-in presets that look most useful for "first time" users:
        # one factory baseline per family + the Maximize combos.
        for preset in BUILT_IN_PRESETS:
            if preset.category in ("model", "maximize", "multicomm"):
                self._source_combo.addItem(f"Preset · {preset.name}", preset)
        self._source_combo.insertSeparator(self._source_combo.count())
        self._source_combo.addItem("Load profile from JSON file…", "_load")
        if allow_use_panel:
            self._source_combo.addItem(
                "Use the values currently in the Settings panel", "_panel"
            )
        source_layout.addWidget(self._source_combo)
        layout.addWidget(source_group)

        self._apply_btn = QPushButton("Apply & Persist")
        self._apply_btn.setStyleSheet(
            "QPushButton { font-weight: bold; padding: 6px 12px; }"
        )
        self._apply_btn.clicked.connect(self._on_apply)
        layout.addWidget(self._apply_btn)

        self._status = QLabel("")
        self._status.setTextFormat(Qt.TextFormat.RichText)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        layout.addStretch(1)

        # Listen for radio_info to populate the connected-radio label.
        self._radio.radio_info.connect(self._on_radio_info)
        self._radio.connected.connect(self._on_connected)
        self._radio.disconnected.connect(self._on_disconnected)

    def reset(self) -> None:
        self._chosen_profile = None
        self._last_report = None
        self._status.setText("")

    def is_complete(self) -> bool:
        return self._last_report is not None and self._last_report.all_applied

    @property
    def report(self):
        return self._last_report

    def _on_radio_info(self, info: object) -> None:
        try:
            board = info.get("board_name")  # type: ignore[union-attr]
            banner = info.get("banner")     # type: ignore[union-attr]
        except Exception:
            return
        self._radio_label.setText(
            f"Connected radio: <b>{board}</b><br>"
            f"<span style='color:#7f8c8d;font-size:11px;'>{banner}</span>"
        )

    def _on_connected(self, port: str, baud: int) -> None:
        self._radio_label.setText(f"Connected on {port} @ {baud} — reading identity…")

    def _on_disconnected(self, reason: str) -> None:
        self._radio_label.setText(
            f"<span style='color:{_WARN_AMBER};'>Radio disconnected: {reason}</span>"
        )

    def set_chosen_profile(self, profile: Profile) -> None:
        """Used by Stage 4 when re-applying the profile saved in Stage 3."""
        self._chosen_profile = profile
        # Add a "Use the profile saved in Stage 3" entry at the top.
        for i in range(self._source_combo.count()):
            if self._source_combo.itemData(i) == "_saved":
                return  # already there
        self._source_combo.insertItem(0, f"Saved profile · {profile.name}", "_saved")
        self._source_combo.setCurrentIndex(0)

    def _resolve_profile(self) -> Profile | None:
        data = self._source_combo.currentData()
        if data == "_load":
            path, _ = QFileDialog.getOpenFileName(
                self, "Load profile", "",
                "JSON profiles (*.json);;All files (*)",
            )
            if not path:
                return None
            try:
                return load_profile(path)
            except Exception as e:
                QMessageBox.critical(self, "Load failed", str(e))
                return None
        if data == "_saved" and self._chosen_profile is not None:
            return self._chosen_profile
        if data == "_panel":
            # Caller (the wizard) hands us the panel snapshot — we don't
            # have direct access, so we synthesise a Profile from the
            # current row values via the apply path. For MVP, refuse and
            # ask user to pick a preset or saved profile instead.
            QMessageBox.information(
                self, "Use panel values",
                "Please pick a preset or saved profile here. "
                "If you want to use your panel's current values, save them as "
                "a user preset first via Profiles → Save current as user preset…",
            )
            return None
        if isinstance(data, Profile):
            return data
        return None

    def _on_apply(self) -> None:
        profile = self._resolve_profile()
        if profile is None:
            return

        # Translate the profile to the radio's actual sreg layout.
        # We need name_to_sreg from somewhere — the Settings tab has it
        # on its panel.  Walk up to the parent wizard to get it.
        from PySide6.QtCore import QObject
        wizard = self.parent()
        while wizard is not None and not isinstance(wizard, PairingWizard):
            wizard = wizard.parent()
        if wizard is None:
            QMessageBox.critical(
                self, "Wizard error",
                "Could not locate the wizard's settings tab reference.",
            )
            return
        name_to_sreg = wizard.name_to_sreg()
        sreg_to_name = wizard.sreg_to_name()
        intended = profile.to_sregs_for(name_to_sreg)
        if not intended:
            QMessageBox.warning(
                self, "Nothing to apply",
                "The selected profile contains no parameters this radio "
                "knows about.",
            )
            return

        dlg = ApplyPersistDialog(
            self._radio,
            intended=intended,
            firmware_banner=wizard.firmware_banner(),
            sreg_to_name=sreg_to_name,
            title=f"{self._title} — apply",
            parent=self,
        )
        # "Stage radio's value" buttons on rejected registers.
        dlg.apply_fix_requested.connect(wizard.stage_fix)
        dlg.exec()
        if dlg.report is None:
            self._status.setText(
                f"<span style='color:{_ERR_RED};'>"
                f"Apply failed or was cancelled.</span>"
            )
            return
        self._last_report = dlg.report
        self._chosen_profile = profile
        if dlg.report.all_applied:
            self._status.setText(
                f"<span style='color:{_OK_GREEN};font-weight:bold;'>"
                f"✓ Applied {len(dlg.report.accepted)} parameter(s) and verified."
                "</span>  You can advance to the next step."
            )
            self.applied.emit(dlg.report)
            self.complete.emit()
        else:
            self._status.setText(
                f"<span style='color:{_WARN_AMBER};'>"
                f"⚠ {len(dlg.report.rejected)} parameter(s) were not applied "
                f"({len(dlg.report.locked)} firmware-locked). "
                "You can continue if your radio is region-locked, or revise "
                "the source and try again.</span>"
            )
            # Don't auto-advance, but expose the report for the wizard.
            self.applied.emit(dlg.report)


class _SaveProfileStage(_Stage):
    saved = Signal(object, str)   # profile, file path

    def __init__(self, radio, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._radio = radio
        self._profile: Profile | None = None
        self._saved_path: Path | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "<h3>Save reference profile</h3>"
            "<p>Now I'll re-read radio A's settings and save them as a JSON "
            "profile. Use this file to configure the second radio (especially "
            "if it's on another PC).</p>"
        ))

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Profile name (used as filename)")
        layout.addWidget(self._name_edit)

        self._capture_btn = QPushButton("Capture and save profile")
        self._capture_btn.clicked.connect(self._on_capture)
        layout.addWidget(self._capture_btn)

        self._status = QLabel("")
        self._status.setTextFormat(Qt.TextFormat.RichText)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._save_copy_btn = QPushButton("Also save a portable copy…")
        self._save_copy_btn.setEnabled(False)
        self._save_copy_btn.clicked.connect(self._on_save_copy)
        layout.addWidget(self._save_copy_btn)

        self._copy_path_btn = QPushButton("Copy file path to clipboard")
        self._copy_path_btn.setEnabled(False)
        self._copy_path_btn.clicked.connect(self._on_copy_path)
        layout.addWidget(self._copy_path_btn)

        layout.addStretch(1)

    def reset(self) -> None:
        # Suggest a default name based on board + timestamp on each entry.
        wizard = self._wizard()
        board = (wizard.board_name() if wizard else "rfd900") or "rfd900"
        self._name_edit.setText(
            f"{board} pair {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        self._profile = None
        self._saved_path = None
        self._status.setText("")
        self._save_copy_btn.setEnabled(False)
        self._copy_path_btn.setEnabled(False)

    def is_complete(self) -> bool:
        return self._profile is not None

    @property
    def profile(self) -> Profile | None:
        return self._profile

    @property
    def saved_path(self) -> Path | None:
        return self._saved_path

    def _wizard(self):
        from PySide6.QtCore import QObject
        w = self.parent()
        while w is not None and not isinstance(w, PairingWizard):
            w = w.parent()
        return w

    def _on_capture(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Save profile", "Please enter a name.")
            return
        wizard = self._wizard()
        if wizard is None:
            return

        def _on_loaded(result: object) -> None:
            radio_info = {
                "banner": wizard.firmware_banner(),
                "board_name": wizard.board_name(),
            }
            try:
                self._profile = profile_from_ati5(name, result, radio_info=radio_info)
                self._saved_path = save_user_profile(self._profile)
            except Exception as e:
                self._status.setText(
                    f"<span style='color:{_ERR_RED};'>Save failed: {e}</span>"
                )
                return
            self._status.setText(
                f"<span style='color:{_OK_GREEN};'>"
                f"✓ Saved {len(self._profile.params)} parameters to:</span> "
                f"<code>{self._saved_path}</code>"
            )
            self._save_copy_btn.setEnabled(True)
            self._copy_path_btn.setEnabled(True)
            self.saved.emit(self._profile, str(self._saved_path))
            self.complete.emit()

        def _on_failed(reason: str) -> None:
            self._status.setText(
                f"<span style='color:{_ERR_RED};'>Could not read the radio: "
                f"{reason}</span>"
            )

        self._status.setText("Re-reading radio config…")
        self._radio.read_params_once(False, _on_loaded, _on_failed)

    def _on_save_copy(self) -> None:
        if self._profile is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save portable copy",
            f"{self._profile.name}.json",
            "JSON profiles (*.json);;All files (*)",
        )
        if path:
            try:
                save_profile(path, self._profile)
                self._status.setText(
                    self._status.text() +
                    f"<br>Portable copy: <code>{path}</code>"
                )
            except Exception as e:
                QMessageBox.critical(self, "Save copy failed", str(e))

    def _on_copy_path(self) -> None:
        if self._saved_path is None:
            return
        QGuiApplication.clipboard().setText(str(self._saved_path))


class _SecondRadioStage(_Stage):
    """Stage 4 — same-PC swap or cross-PC instructions."""

    applied_to_remote = Signal()

    def __init__(self, radio, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._radio = radio
        self._mode = "same_pc"
        self._reference_profile: Profile | None = None
        self._reference_path: Path | None = None
        self._waiting_for_swap = False
        self._original_port = ""

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h3>Configure the second radio</h3>"))

        self._guidance = QLabel("")
        self._guidance.setTextFormat(Qt.TextFormat.RichText)
        self._guidance.setWordWrap(True)
        layout.addWidget(self._guidance)

        # Same-PC: re-uses the apply stage embedded inline
        self._configure_stage = _ConfigureStage(
            radio,
            "Apply same profile to radio B",
            allow_use_panel=False,
        )
        layout.addWidget(self._configure_stage)
        self._configure_stage.applied.connect(self._on_b_applied)
        self._configure_stage.complete.connect(self._on_b_complete)
        self._configure_stage.setVisible(False)

        # Cross-PC: explicit confirm button
        self._cross_pc_btn = QPushButton(
            "I've applied the profile on the other PC"
        )
        self._cross_pc_btn.setVisible(False)
        self._cross_pc_btn.clicked.connect(self.complete.emit)
        layout.addWidget(self._cross_pc_btn)

        layout.addStretch(1)

        # Watch for radio swap (disconnect → connected on different port)
        self._radio.disconnected.connect(self._on_disconnected)
        self._radio.connected.connect(self._on_connected)

    def configure(
        self,
        mode: str,
        profile: Profile,
        saved_path: Path | None,
    ) -> None:
        self._mode = mode
        self._reference_profile = profile
        self._reference_path = saved_path
        self._render()

    def _render(self) -> None:
        if self._mode == "same_pc":
            self._guidance.setText(
                "<p><b>Step 1.</b> Disconnect radio A from USB.</p>"
                "<p><b>Step 2.</b> Connect radio B (the second radio) to the "
                "<i>same</i> USB port.</p>"
                "<p><b>Step 3.</b> Wait for the connection panel to show the "
                "new radio, then click <b>Apply & Persist</b> below to push "
                "the same profile.</p>"
            )
            self._configure_stage.setVisible(True)
            self._cross_pc_btn.setVisible(False)
            if self._reference_profile is not None:
                self._configure_stage.set_chosen_profile(self._reference_profile)
        else:
            ref = (
                f"<code>{self._reference_path}</code>"
                if self._reference_path
                else "(see Stage 3)"
            )
            self._guidance.setText(
                "<p><b>To configure radio B on the other PC:</b></p>"
                f"<ol>"
                f"<li>Copy the JSON file ({ref}) to the other PC "
                f"(USB stick, shared folder, etc).</li>"
                f"<li>On that PC, install rfdtool and connect to radio B.</li>"
                f"<li>Profiles ▾ → <b>Import preset from JSON…</b> → pick "
                f"the file you just copied.</li>"
                f"<li>That'll stage the values; then run <b>Save Settings</b> "
                f"and <b>Save EEPROM</b> and <b>Reboot</b> (in that order). "
                f"Or use the wizard there too — same as Stage 2.</li>"
                f"<li>Come back here and click below when done.</li>"
                f"</ol>"
            )
            self._configure_stage.setVisible(False)
            self._cross_pc_btn.setVisible(True)

    def _on_disconnected(self, _reason: str) -> None:
        if self._mode == "same_pc":
            self._waiting_for_swap = True
            self._guidance.setText(
                self._guidance.text() +
                f"<p><span style='color:{_WARN_AMBER};'>Waiting for "
                "radio B…</span></p>"
            )

    def _on_connected(self, port: str, _baud: int) -> None:
        if self._mode == "same_pc" and self._waiting_for_swap:
            self._waiting_for_swap = False
            self._guidance.setText(
                self._guidance.text() +
                f"<p><span style='color:{_OK_GREEN};'>"
                f"✓ Radio B detected on {port}.</span> Click "
                "Apply & Persist below to push the same profile.</p>"
            )

    def _on_b_applied(self, _report) -> None:
        pass

    def _on_b_complete(self) -> None:
        self.applied_to_remote.emit()
        self.complete.emit()

    def is_complete(self) -> bool:
        if self._mode == "same_pc":
            return self._configure_stage.is_complete()
        return False  # cross-PC needs the explicit button click


class _VerifyLinkStage(_Stage):
    """Stage 5 — watch ATI7 / RADIO_STATUS for link-up confirmation."""

    POLL_INTERVAL_MS = 2500
    PASS_TIMEOUT_S = 30.0

    def __init__(self, radio, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._radio = radio
        self._reference_profile: Profile | None = None
        self._link_up = False
        self._start_t = 0.0
        self._poll_future = None
        self._active = False

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h3>Verify the link</h3>"))
        layout.addWidget(QLabel(
            "<p>Both radios should now be configured identically. I'm "
            "polling RSSI/packet stats — give it ~30 seconds for the "
            "TDM hop sequence to sync.</p>"
        ))

        self._status_lbl = QLabel("Waiting for first poll…")
        self._status_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._status_lbl.setWordWrap(True)
        layout.addWidget(self._status_lbl)

        self._progress = QProgressBar()
        self._progress.setRange(0, int(self.PASS_TIMEOUT_S))
        self._progress.setValue(0)
        layout.addWidget(self._progress)

        self._compare_btn = QPushButton(
            "Diff against reference profile (when no link)"
        )
        self._compare_btn.setEnabled(False)
        self._compare_btn.clicked.connect(self._on_compare)
        layout.addWidget(self._compare_btn)

        layout.addStretch(1)

        self._poll = QTimer(self)
        self._poll.setInterval(self.POLL_INTERVAL_MS)
        self._poll.timeout.connect(self._tick)

        self._radio.rssi_received.connect(self._on_rssi)
        self._radio.mavlink_radio_status.connect(self._on_mavlink)

    def deactivate(self) -> None:
        """Stop polling and ignore link reports (stage left or wizard closed)."""
        self._active = False
        self._poll.stop()

    def reset(self) -> None:
        self._active = True
        self._link_up = False
        self._start_t = time.monotonic()
        self._status_lbl.setText("Waiting for first poll…")
        self._progress.setValue(0)
        self._compare_btn.setEnabled(False)
        self._poll.start()
        QTimer.singleShot(0, self._tick)

    def is_complete(self) -> bool:
        return self._link_up

    def set_reference_profile(self, profile: Profile) -> None:
        self._reference_profile = profile

    def _tick(self) -> None:
        if not self._active:
            return
        elapsed = time.monotonic() - self._start_t
        self._progress.setValue(int(min(elapsed, self.PASS_TIMEOUT_S)))
        if elapsed >= self.PASS_TIMEOUT_S:
            self._poll.stop()
            self._status_lbl.setText(
                f"<span style='color:{_WARN_AMBER};font-weight:bold;'>"
                "⚠ No link after 30 s.</span>  Click below to compare this "
                "radio's settings against the reference profile and stage any "
                "missing fixes."
            )
            self._compare_btn.setEnabled(True)
            return
        # Trigger a fresh ATI7 (MultiComm: AT&T=RSSI / RTI) read, unless the
        # previous one is still running — MultiComm polls take >2 s.
        if self._poll_future is not None and not self._poll_future.done():
            return
        self._poll_future = self._radio.poll_rssi()

    def _on_rssi(self, report) -> None:
        # Reports also arrive from the RSSI tab and while other stages are
        # showing; only react while this stage is live and still waiting.
        if not self._active or self._link_up:
            return
        local = getattr(report, "local_rssi", None)
        remote = getattr(report, "remote_rssi", None)
        pkts = getattr(report, "pkts", None)
        rxe = getattr(report, "rxe", None)
        unit = " dBm" if getattr(report, "units", "raw") == "dBm" else ""
        text = (
            f"L/R RSSI{unit}: <b>{local if local is not None else '—'}</b>/"
            f"<b>{remote if remote is not None else '—'}</b>"
            + (f"  pkts: <b>{pkts}</b>" if pkts is not None else "")
            + (f"  rxerr: {rxe}" if rxe is not None else "")
        )
        linked = getattr(report, "linked", None)
        if linked is None:
            linked = bool(pkts and pkts > 0 and remote and remote > 0)
        if linked:
            self._link_up = True
            self._poll.stop()
            self._status_lbl.setText(
                f"<span style='color:{_OK_GREEN};font-weight:bold;'>"
                f"✓ LINKED.</span>  {text}"
            )
            self.complete.emit()
        else:
            self._status_lbl.setText(text)

    def _on_mavlink(self, msg) -> None:
        # MAVLink RADIO_STATUS gives us another path to "linked" detection
        # when the user has MAVLink framing on.
        if not self._active or self._link_up:
            return
        rssi = getattr(msg, "rssi", 0)
        remrssi = getattr(msg, "remrssi", 0)
        if rssi and remrssi:
            self._link_up = True
            self._poll.stop()
            self._status_lbl.setText(
                f"<span style='color:{_OK_GREEN};font-weight:bold;'>"
                f"✓ LINKED via MAVLink RADIO_STATUS</span>  "
                f"L={rssi} R={remrssi}"
            )
            self.complete.emit()

    def _on_compare(self) -> None:
        wizard = self.parent()
        from PySide6.QtCore import QObject
        while wizard is not None and not isinstance(wizard, PairingWizard):
            wizard = wizard.parent()
        if wizard is None or self._reference_profile is None:
            QMessageBox.warning(
                self, "Compare", "No reference profile available."
            )
            return
        # Snapshot local panel via fresh ATI5; once loaded, build diff.
        def _on_loaded(result):
            local_params = {}
            local_sregs = {}
            for sreg, name in (getattr(result, "s_names", {}) or {}).items():
                if name:
                    local_params[name] = (getattr(result, "s_params", {}) or {}).get(sreg)
                    local_sregs[name] = sreg
            ref_sregs = wizard.name_to_sreg()
            dlg = CompareConfigsDialog(
                "Local (this radio)", local_params, local_sregs,
                "Reference profile", dict(self._reference_profile.params), ref_sregs,
                title="Pairing diagnostic",
                family=getattr(result, "family", None),
                # The reference came from radio A; this is radio B.
                peer=True,
                parent=self,
            )
            dlg.apply_fix_requested.connect(wizard.stage_fix)
            dlg.exec()

        def _on_failed(reason: str) -> None:
            QMessageBox.warning(self, "Compare", f"Could not read the radio: {reason}")

        self._radio.read_params_once(False, _on_loaded, _on_failed)


class PairingWizard(QDialog):
    """The 5-stage modal pairing wizard."""

    apply_fix_requested = Signal(int, object)   # sreg, value (may be a tuple)

    def __init__(
        self,
        radio,
        *,
        name_to_sreg_provider,    # callable returning current local panel name_to_sreg
        sreg_to_name_provider,    # ditto sreg_to_name
        firmware_banner_provider, # callable returning banner string
        board_name_provider,      # callable returning board name
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Radio pairing wizard")
        self.setModal(True)
        self.resize(720, 540)

        self._radio = radio
        self._name_to_sreg = name_to_sreg_provider
        self._sreg_to_name = sreg_to_name_provider
        self._firmware_banner = firmware_banner_provider
        self._board_name = board_name_provider

        self._stages: list[_Stage] = [
            _WelcomeStage(),
            _ConfigureStage(radio, "Configure radio A"),
            _SaveProfileStage(radio),
            _SecondRadioStage(radio),
            _VerifyLinkStage(radio),
        ]
        self._stack = QStackedWidget()
        for s in self._stages:
            self._stack.addWidget(s)
            s.complete.connect(self._on_stage_complete)
        # Capture saved profile from Stage 3 → propagate to 4 + 5
        self._stages[2].saved.connect(self._on_profile_saved)

        root = QVBoxLayout(self)
        self._stage_lbl = QLabel("")
        self._stage_lbl.setStyleSheet("font-size: 12px; color: #7f8c8d;")
        root.addWidget(self._stage_lbl)
        root.addWidget(self._stack, 1)

        nav = QHBoxLayout()
        self._back_btn = QPushButton("Back")
        self._back_btn.clicked.connect(self._on_back)
        self._next_btn = QPushButton("Next")
        self._next_btn.clicked.connect(self._on_next)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self.reject)
        nav.addWidget(self._back_btn)
        nav.addStretch(1)
        nav.addWidget(self._cancel_btn)
        nav.addWidget(self._next_btn)
        root.addLayout(nav)

        self._goto(0)

    # ---------- providers exposed to stages ----------
    def name_to_sreg(self) -> dict[str, int]:
        return self._name_to_sreg()

    def sreg_to_name(self) -> dict[int, str]:
        return self._sreg_to_name()

    def firmware_banner(self) -> str:
        return self._firmware_banner()

    def board_name(self) -> str:
        return self._board_name()

    def stage_fix(self, sreg: int, value: object) -> None:
        self.apply_fix_requested.emit(sreg, value)

    # ---------- navigation ----------
    def _goto(self, idx: int) -> None:
        idx = max(0, min(idx, len(self._stages) - 1))
        for other in self._stages:
            if other is not self._stages[idx] and hasattr(other, "deactivate"):
                other.deactivate()
        self._stack.setCurrentIndex(idx)
        stage = self._stages[idx]
        stage.reset()
        self._stage_lbl.setText(f"Step {idx + 1} of {len(self._stages)}")
        self._update_nav()

    def _update_nav(self) -> None:
        idx = self._stack.currentIndex()
        self._back_btn.setEnabled(idx > 0)
        # Special-case Stage 4 setup based on Stage 1's mode.
        if idx == 3 and isinstance(self._stages[3], _SecondRadioStage):
            stage4: _SecondRadioStage = self._stages[3]   # type: ignore[assignment]
            stage1: _WelcomeStage = self._stages[0]        # type: ignore[assignment]
            stage3: _SaveProfileStage = self._stages[2]    # type: ignore[assignment]
            if stage3.profile is not None:
                stage4.configure(stage1.mode(), stage3.profile, stage3.saved_path)
        # Stage 5 needs the reference profile.
        if idx == 4:
            stage5: _VerifyLinkStage = self._stages[4]     # type: ignore[assignment]
            stage3 = self._stages[2]                       # type: ignore[assignment]
            if stage3.profile is not None:
                stage5.set_reference_profile(stage3.profile)
        is_last = idx == len(self._stages) - 1
        self._next_btn.setText("Done" if is_last else "Next")
        # Always allow Next; specific stages can still gate via dialogs.
        self._next_btn.setEnabled(True)

    def _on_back(self) -> None:
        idx = self._stack.currentIndex()
        if idx > 0:
            self._goto(idx - 1)

    def _on_next(self) -> None:
        idx = self._stack.currentIndex()
        is_last = idx == len(self._stages) - 1
        if is_last:
            self.accept()
        else:
            self._goto(idx + 1)

    def _on_stage_complete(self) -> None:
        # When a stage completes, advance automatically — but never out of
        # the final stage (user clicks Done), and only if the stage that
        # completed is the one on screen (a background stage must not
        # drag the wizard forward).
        idx = self._stack.currentIndex()
        if self.sender() is not self._stages[idx]:
            return
        if idx < len(self._stages) - 1:
            QTimer.singleShot(
                0, lambda: self._stack.currentIndex() == idx and self._goto(idx + 1)
            )

    def done(self, result: int) -> None:  # noqa: D401 — Qt override
        # Accept, reject and window close all land here: stop background
        # polling so a closed wizard can't keep driving the radio.
        for stage in self._stages:
            if hasattr(stage, "deactivate"):
                stage.deactivate()
        super().done(result)

    def _on_profile_saved(self, profile: Profile, path: str) -> None:
        # Pre-configure stage 4 + 5 immediately so they're ready when the
        # user advances.
        stage4: _SecondRadioStage = self._stages[3]         # type: ignore[assignment]
        stage5: _VerifyLinkStage = self._stages[4]          # type: ignore[assignment]
        stage1: _WelcomeStage = self._stages[0]             # type: ignore[assignment]
        stage4.configure(stage1.mode(), profile, Path(path))
        stage5.set_reference_profile(profile)

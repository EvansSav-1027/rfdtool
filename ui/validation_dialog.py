"""Modal dialogs for the Settings tab.

* :class:`ApplyPresetDialog` — shown before applying a built-in or user
  preset to the local panel.  Renders a diff (current → new) plus the
  preset's notes, and warns when the preset doesn't list the detected
  board in its ``applies_to``.
* :class:`ValidationDialog` — shown after the user clicks "Validate".
  Lists every issue grouped by severity, with click-to-fix buttons for
  issues whose underlying rule produced a `suggested_value`.
* :class:`SavePresetDialog` — used by "Save current as user preset…".
  Collects name/description/notes and emits the new :class:`Profile`.
"""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from rfd.presets import Profile
from rfd.registers import FAMILY_MULTICOMM, REGISTERS, ParamValue, RegisterDef
from rfd.validation import ValidationIssue, ValidationReport


_SEVERITY_COLOURS = {
    "error": "#c0392b",
    "warning": "#d68910",
    "info": "#2874a6",
}


_SEVERITY_ICONS = {
    "error": "✕",
    "warning": "⚠",
    "info": "ⓘ",
}


def _format_value(reg: RegisterDef | None, value: ParamValue) -> str:
    """Render a value the same way the editor would (enum label + numeric)."""
    if isinstance(value, tuple):
        text = ",".join(map(str, value))
        return text if len(text) <= 48 else text[:45] + "…"
    if reg is None:
        return str(value)
    if reg.kind in ("enum", "bool") and reg.enum:
        label = reg.enum.get(value)
        if label is not None:
            return f"{label} ({value})"
    if reg.units:
        return f"{value} {reg.units}"
    return str(value)


# --------------------------------------------------------------------- Apply preset
class ApplyPresetDialog(QDialog):
    """Confirm-and-apply dialog for a preset.  Shows the diff and notes."""

    def __init__(
        self,
        profile: Profile,
        current_values: dict[int, ParamValue],
        *,
        board_name: str = "",
        name_to_sreg: dict[str, int] | None = None,
        sreg_to_name: dict[int, str] | None = None,
        reg_defs: dict[int, RegisterDef] | None = None,
        family: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Apply preset — {profile.name}")
        self.setModal(True)
        self.resize(620, 480)
        self._profile = profile

        # Resolve the name → sreg map. Defaults to canonical SiK so this
        # dialog still works when called without explicit mapping.
        if name_to_sreg is None:
            from rfd.registers import CANONICAL_SIK_NAMES
            name_to_sreg = {n: s for s, n in CANONICAL_SIK_NAMES.items()}
        if sreg_to_name is None:
            sreg_to_name = {s: n for n, s in name_to_sreg.items()}

        root = QVBoxLayout(self)

        # Header — preset name + category + applies-to mismatch warning
        header = QLabel(f"<b>{profile.name}</b>")
        header.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(header)

        if profile.description:
            desc = QLabel(profile.description)
            desc.setWordWrap(True)
            root.addWidget(desc)

        applies_text = (
            "Applies to: any board"
            if not profile.applies_to
            else f"Applies to: {', '.join(profile.applies_to)}"
        )
        root.addWidget(QLabel(applies_text))

        if profile.applies_to and board_name and not profile.matches_board(board_name):
            warn = QLabel(
                f"<span style='color:{_SEVERITY_COLOURS['warning']};'>"
                f"⚠ This preset is not tagged for the connected radio "
                f"({board_name}). Apply anyway only if you're sure.</span>"
            )
            warn.setTextFormat(Qt.TextFormat.RichText)
            warn.setWordWrap(True)
            root.addWidget(warn)

        if not profile.matches_firmware(family):
            fw = "MultiComm" if profile.firmware == FAMILY_MULTICOMM else "SiK"
            here = "MultiComm" if family == FAMILY_MULTICOMM else "SiK"
            warn = QLabel(
                f"<span style='color:{_SEVERITY_COLOURS['error']};'>"
                f"✕ This preset is written for {fw} firmware but the radio runs "
                f"{here}. Parameter names don't carry over — most of it will be "
                f"skipped.</span>"
            )
            warn.setTextFormat(Qt.TextFormat.RichText)
            warn.setWordWrap(True)
            root.addWidget(warn)

        # Diff list — walk preset by NAME and translate to the radio's sreg.
        diff_label = QLabel("<b>Changes that will be staged:</b>")
        diff_label.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(diff_label)

        diff_scroll = QScrollArea()
        diff_scroll.setWidgetResizable(True)
        diff_widget = QWidget()
        diff_layout = QVBoxLayout(diff_widget)
        diff_layout.setContentsMargins(6, 6, 6, 6)
        diff_layout.setSpacing(2)

        any_change = False
        skipped: list[str] = []
        for name in sorted(profile.params):
            new_val = profile.params[name]
            sreg = name_to_sreg.get(name)
            if sreg is None:
                # Parameter exists in the preset but not on this firmware.
                skipped.append(name)
                continue
            reg = (reg_defs or REGISTERS).get(sreg)
            if reg is None or reg.read_only:
                continue
            cur_val = current_values.get(sreg)
            if cur_val == new_val:
                continue
            cur_str = _format_value(reg, cur_val) if cur_val is not None else "(unset)"
            new_str = _format_value(reg, new_val)
            firmware_name = sreg_to_name.get(sreg, name)
            line = QLabel(
                f"<code>S{sreg:<2}</code> {firmware_name}: "
                f"<span style='color:#888;'>{cur_str}</span> → "
                f"<b>{new_str}</b>"
            )
            line.setTextFormat(Qt.TextFormat.RichText)
            diff_layout.addWidget(line)
            any_change = True

        if not any_change:
            diff_layout.addWidget(QLabel("(no changes — current values already match)"))

        if skipped:
            skip_lbl = QLabel(
                f"<span style='color:#888;'>"
                f"<i>Will be skipped (not present on this radio): "
                f"{', '.join(skipped)}</i></span>"
            )
            skip_lbl.setTextFormat(Qt.TextFormat.RichText)
            skip_lbl.setWordWrap(True)
            diff_layout.addWidget(skip_lbl)

        diff_layout.addStretch(1)
        diff_scroll.setWidget(diff_widget)
        root.addWidget(diff_scroll, 1)

        # Notes
        if profile.notes:
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            root.addWidget(sep)
            notes = QLabel(f"<i>{profile.notes}</i>")
            notes.setTextFormat(Qt.TextFormat.RichText)
            notes.setWordWrap(True)
            root.addWidget(notes)

        # Special warning if SERIAL_SPEED will change — that breaks the
        # current connection on the next save.
        baud_param = "S1BAUD" if family == FAMILY_MULTICOMM else "SERIAL_SPEED"
        baud_sreg = name_to_sreg.get(baud_param)
        baud_new = profile.params.get(baud_param)
        if baud_sreg is not None and baud_new is not None and current_values.get(baud_sreg) != baud_new:
            note = QLabel(
                f"<span style='color:{_SEVERITY_COLOURS['warning']};'>"
                f"⚠ This preset changes {baud_param}. After saving, "
                f"you'll need to reconnect at the new baud.</span>"
            )
            note.setTextFormat(Qt.TextFormat.RichText)
            note.setWordWrap(True)
            root.addWidget(note)

        # Action buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.button(QDialogButtonBox.StandardButton.Apply).setText("Apply (stage as unsaved)")
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).clicked.connect(self.reject)
        root.addWidget(buttons)


# --------------------------------------------------------------------- Validation summary
class ValidationDialog(QDialog):
    """Summary of validation issues with optional click-to-fix actions."""

    # object, not int: suggested values may be tuples or exceed 32 bits.
    apply_fix_requested = Signal(int, object)  # sreg, suggested_value

    def __init__(
        self,
        report: ValidationReport,
        *,
        reg_defs: dict[int, RegisterDef] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Validation results")
        self.setModal(True)
        self.resize(720, 540)
        self._report = report
        self._reg_defs = reg_defs or REGISTERS

        root = QVBoxLayout(self)

        # Header — overall summary line
        e, w, i = len(report.errors), len(report.warnings), len(report.infos)
        if report.overall == "ok":
            header_text = (
                f"<b style='color:#27ae60;'>✓ All settings look good.</b>"
            )
        else:
            parts = []
            if e:
                parts.append(f"<span style='color:{_SEVERITY_COLOURS['error']};'>"
                             f"✕ {e} error{'s' if e != 1 else ''}</span>")
            if w:
                parts.append(f"<span style='color:{_SEVERITY_COLOURS['warning']};'>"
                             f"⚠ {w} warning{'s' if w != 1 else ''}</span>")
            if i:
                parts.append(f"<span style='color:{_SEVERITY_COLOURS['info']};'>"
                             f"ⓘ {i} info</span>")
            header_text = " · ".join(parts)
        header = QLabel(header_text)
        header.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(header)

        if report.detected_region is not None:
            r = report.detected_region
            root.addWidget(QLabel(
                f"Detected band: <b>{r.name}</b> ({r.citation})"
            ))
        elif any(
            {"MIN_FREQ", "MAX_FREQ", "FREQMIN", "FREQMAX"} & set(i.param_names)
            for i in report.issues
        ):
            root.addWidget(QLabel(
                "<i>Detected band: none — frequency range doesn't match any known region.</i>"
            ))

        if report.detected_board:
            root.addWidget(QLabel(f"Connected radio: <b>{report.detected_board}</b>"))

        # Issue list
        issues_scroll = QScrollArea()
        issues_scroll.setWidgetResizable(True)
        issues_widget = QWidget()
        issues_layout = QVBoxLayout(issues_widget)
        issues_layout.setContentsMargins(6, 6, 6, 6)
        issues_layout.setSpacing(8)

        if report.overall == "ok":
            ok_lbl = QLabel("<i>No rules flagged any concerns. Click Save Settings to apply.</i>")
            ok_lbl.setTextFormat(Qt.TextFormat.RichText)
            issues_layout.addWidget(ok_lbl)
        else:
            # Group: errors, warnings, infos in that order
            for severity, label in (("error", "Errors"), ("warning", "Warnings"), ("info", "Information")):
                bucket = [i for i in report.issues if i.severity == severity]
                if not bucket:
                    continue
                hdr = QLabel(
                    f"<b style='color:{_SEVERITY_COLOURS[severity]};'>"
                    f"{_SEVERITY_ICONS[severity]} {label} ({len(bucket)})</b>"
                )
                hdr.setTextFormat(Qt.TextFormat.RichText)
                issues_layout.addWidget(hdr)
                for issue in bucket:
                    issues_layout.addWidget(self._build_issue_widget(issue))

        issues_layout.addStretch(1)
        issues_scroll.setWidget(issues_widget)
        root.addWidget(issues_scroll, 1)

        # Footer
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        root.addWidget(buttons)

    def _build_issue_widget(self, issue: ValidationIssue) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        sregs_label = ""
        if issue.sregs:
            sregs_label = " — " + ", ".join(f"S{s}" for s in issue.sregs)
        title = QLabel(
            f"<b>{_SEVERITY_ICONS[issue.severity]} {issue.title}</b>"
            f"<span style='color:#888;'>{sregs_label}</span>"
        )
        title.setTextFormat(Qt.TextFormat.RichText)
        title.setWordWrap(True)
        layout.addWidget(title)

        detail = QLabel(issue.detail)
        detail.setWordWrap(True)
        layout.addWidget(detail)

        if issue.citation:
            cite = QLabel(f"<i>Source: {issue.citation}</i>")
            cite.setTextFormat(Qt.TextFormat.RichText)
            cite.setWordWrap(True)
            layout.addWidget(cite)

        # Apply-fix row
        if issue.suggested_value is not None and issue.sregs:
            row = QHBoxLayout()
            target_sreg = issue.sregs[0]
            reg = self._reg_defs.get(target_sreg)
            fix_label = QLabel(
                issue.fix_hint
                or f"Set S{target_sreg} to {_format_value(reg, issue.suggested_value)}."
            )
            fix_label.setWordWrap(True)
            row.addWidget(fix_label, 1)
            apply_btn = QPushButton("Apply fix")
            apply_btn.clicked.connect(
                lambda *, s=target_sreg, v=issue.suggested_value:
                    self.apply_fix_requested.emit(s, v)
            )
            row.addWidget(apply_btn)
            layout.addLayout(row)

        return frame


# --------------------------------------------------------------------- Save user preset
class SavePresetDialog(QDialog):
    """Collect name/description/notes/applies_to for a user preset."""

    def __init__(
        self,
        *,
        suggested_name: str = "",
        board_name: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Save preset")
        self.setModal(True)
        self.resize(480, 360)
        self._board_name = board_name

        layout = QFormLayout(self)

        self._name = QLineEdit(suggested_name)
        layout.addRow("Name:", self._name)

        self._description = QLineEdit()
        self._description.setPlaceholderText("Short summary shown in the preset menu")
        layout.addRow("Description:", self._description)

        self._notes = QPlainTextEdit()
        self._notes.setPlaceholderText("Free-form notes — e.g. mission profile, antenna setup, citations")
        self._notes.setMaximumHeight(120)
        layout.addRow("Notes:", self._notes)

        self._tag_board = QCheckBox(
            f"Tag as applicable only to {board_name}"
            if board_name else "Tag for a specific board (none detected)"
        )
        self._tag_board.setChecked(False)
        self._tag_board.setEnabled(bool(board_name))
        layout.addRow("", self._tag_board)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).clicked.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).clicked.connect(self.reject)
        layout.addRow(buttons)

    def name(self) -> str:
        return self._name.text().strip()

    def description(self) -> str:
        return self._description.text().strip()

    def notes(self) -> str:
        return self._notes.toPlainText().strip()

    def applies_to(self) -> list[str]:
        if self._tag_board.isChecked() and self._board_name:
            return [self._board_name]
        return []

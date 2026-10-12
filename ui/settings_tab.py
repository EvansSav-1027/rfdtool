"""Two-panel S-register editor for local + remote RFD900 radios.

Renders every S-register and pin-mapping register (R0..R15) for both the
local and the remote radio, side-by-side, as live editor widgets bound to a
:class:`rfd.radio.Radio`. Loads parameters via ATI5/RTI5, tracks per-row
dirty state, and writes only the modified registers on demand. Built-in
profiles and JSON profile import/export are exposed via a Profiles menu.

Works with SiK and RFD MultiComm firmware.  For MultiComm the rows are
grouped into sections, array registers and the 64-bit SLOTIDS mask get text
editors, ranges come from the radio itself (ATI10), and a node selector
addresses RT commands to one NODEID.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from rfd.presets import (
    BUILT_IN_PRESETS,
    CATEGORY_MAXIMIZE,
    CATEGORY_MODEL,
    CATEGORY_MULTICOMM,
    CATEGORY_REGION,
    CATEGORY_USE_CASE,
    Profile,
    delete_user_profile,
    list_user_profiles,
    load_profile,
    presets_by_category,
    profile_from_ati5,
    save_profile,
    save_user_profile,
    user_profile_dir,
)
from rfd import multicomm
from rfd.radio import Radio
from rfd.registers import (
    CANONICAL_PIN_NAMES,
    CANONICAL_SIK_NAMES,
    FAMILY_MULTICOMM,
    FAMILY_SIK,
    ParamMeta,
    ParamValue,
    RegisterDef,
    all_pin_registers,
    all_registers,
    coerce_value,
    derive_def,
    validate_def,
)
from rfd.validation import ValidationReport, validate_config

from .validation_dialog import ApplyPresetDialog, SavePresetDialog, ValidationDialog


# Connected states in which the action buttons are usable. Anything else
# (disconnected/bootloader) means the radio cannot service AT commands.
_LIVE_STATES: frozenset[str] = frozenset({Radio.STATE_DATA, Radio.STATE_COMMAND})

# Editor background tints for the various row states. Dirty trumps validation
# tints because un-saved edits matter more than stale validation results.
_DIRTY_QSS = "background-color: #fff4c2;"
_VALIDATION_QSS = {
    "error":   "background-color: #ffd6d6;",
    "warning": "background-color: #ffe4b5;",
    "info":    "background-color: #e0f0ff;",
    "ok":      "background-color: #d4edda;",
}


@dataclass
class _Row:
    """One register row inside a panel."""

    sreg: int
    is_pin: bool
    reg: RegisterDef
    container: QWidget          # the row's outer QWidget — used by rebuild to
                                # delete just this row without touching the
                                # pin-group widget that sits in the same layout
    label: QLabel
    editor: QWidget
    dirty_marker: QLabel
    status_marker: QLabel
    units_label: QLabel | None = None
    is_dirty: bool = False
    validation_severity: str = ""   # "" | "error" | "warning" | "info" | "ok"
    validation_titles: tuple[str, ...] = ()

    def value(self) -> ParamValue | None:
        """Current editor value, or None if a text editor holds something
        that doesn't parse (the save path reports that row)."""
        if isinstance(self.editor, QSpinBox):
            return int(self.editor.value())
        if isinstance(self.editor, QComboBox):
            data = self.editor.currentData()
            return int(data) if data is not None else 0
        if isinstance(self.editor, QLineEdit):
            text = self.editor.text().strip()
            try:
                if self.reg.kind == "list":
                    return coerce_value(text.rstrip(",") + ",") if text else ()
                # Decimal, or 0x-hex for masks; int(text, 0) would reject
                # leading zeros like "08".
                return int(text, 16) if text[:2].lower() == "0x" else int(text)
            except ValueError:
                return None
        return 0

    def refresh_hint(self) -> None:
        """Live description next to the SLOTIDS mask editor."""
        if self.reg.kind != "bitmask" or self.units_label is None:
            return
        v = self.value()
        self.units_label.setText(
            multicomm.describe_slots(v) if isinstance(v, int) else "invalid"
        )

    def set_value(self, value: ParamValue) -> None:
        # Block signals so programmatic updates from the radio don't mark the
        # row dirty.
        self.editor.blockSignals(True)
        try:
            if isinstance(self.editor, QLineEdit):
                if isinstance(value, tuple):
                    self.editor.setText(",".join(str(v) for v in value))
                else:
                    self.editor.setText(str(int(value)))
                self.refresh_hint()
            elif isinstance(self.editor, QSpinBox):
                v = int(value)
                # Auto-expand the spinbox range if the radio reports a value
                # outside our current bounds (e.g. newer firmware widening a
                # register that used to cap lower). Without this the QSpinBox
                # silently clips and the user sees a stale display value.
                v = max(_INT32_MIN, min(v, _INT32_MAX))
                if v < self.editor.minimum():
                    self.editor.setMinimum(v)
                if v > self.editor.maximum():
                    self.editor.setMaximum(v)
                self.editor.setValue(v)
            elif isinstance(self.editor, QComboBox):
                idx = self.editor.findData(int(value))
                if idx < 0:
                    # Unknown enum option — append it so the value round-trips.
                    self.editor.addItem(f"{value} (unknown)", int(value))
                    idx = self.editor.findData(int(value))
                self.editor.setCurrentIndex(idx)
        finally:
            self.editor.blockSignals(False)

    def apply_visual(self) -> None:
        """Refresh the editor's background and the status marker to reflect
        ``is_dirty`` and ``validation_severity``. Dirty wins over validation."""
        if self.is_dirty:
            self.editor.setStyleSheet(_DIRTY_QSS)
            self.dirty_marker.setText("*")
        else:
            self.dirty_marker.setText("")
            qss = _VALIDATION_QSS.get(self.validation_severity, "")
            self.editor.setStyleSheet(qss)
        icon = {"error": "✕", "warning": "⚠", "info": "ⓘ", "ok": "✓"}.get(
            self.validation_severity, ""
        )
        colour = {
            "error": "#c0392b", "warning": "#d68910",
            "info": "#2874a6", "ok": "#27ae60",
        }.get(self.validation_severity, "")
        self.status_marker.setText(icon)
        self.status_marker.setStyleSheet(
            f"color: {colour}; font-weight: bold;" if colour else ""
        )
        if self.validation_titles:
            self.status_marker.setToolTip("\n".join(self.validation_titles))
        else:
            self.status_marker.setToolTip("")


_INT32_MIN, _INT32_MAX = -(2**31), 2**31 - 1


def _make_editor(reg: RegisterDef, parent: QWidget) -> QWidget:
    """Build the editor widget appropriate for ``reg.kind``."""
    lo = reg.minimum if reg.minimum is not None else 0
    hi = reg.maximum if reg.maximum is not None else 0xFFFF
    if reg.kind == "int" and _INT32_MIN <= lo and hi <= _INT32_MAX:
        sb = QSpinBox(parent)
        sb.setRange(int(lo), int(hi))
        if reg.default is not None:
            sb.setValue(int(reg.default))
        if reg.units:
            sb.setSuffix(f" {reg.units}")
        sb.setToolTip(reg.tooltip)
        if reg.read_only:
            sb.setEnabled(False)
        return sb

    if reg.kind == "enum":
        cb = QComboBox(parent)
        if reg.enum:
            for k in sorted(reg.enum.keys()):
                cb.addItem(f"{reg.enum[k]} ({k})", int(k))
        if reg.default is not None:
            idx = cb.findData(int(reg.default))
            if idx >= 0:
                cb.setCurrentIndex(idx)
        cb.setToolTip(reg.tooltip)
        if reg.read_only:
            cb.setEnabled(False)
        return cb

    if reg.kind == "bool":
        cb = QComboBox(parent)
        cb.addItem("Off (0)", 0)
        cb.addItem("On (1)", 1)
        if reg.default is not None:
            cb.setCurrentIndex(1 if int(reg.default) else 0)
        cb.setToolTip(reg.tooltip)
        if reg.read_only:
            cb.setEnabled(False)
        return cb

    if reg.kind in ("list", "bitmask", "int"):
        # "int" lands here only when its range exceeds QSpinBox's int32.
        # Arrays (MultiComm ENCRYPTKEY, RCDEFS…) and SLOTIDS (64-bit, beyond
        # QSpinBox's int32) are edited as text.
        le = QLineEdit(parent)
        if reg.kind == "list":
            le.setPlaceholderText(
                f"{reg.length or ''} comma-separated values "
                f"({reg.minimum}..{reg.maximum})".strip()
            )
        elif reg.kind == "bitmask":
            le.setPlaceholderText("decimal bitmask (bit 0 = slot 1)")
        else:
            le.setPlaceholderText(f"integer {reg.minimum}..{reg.maximum}")
        if reg.default is not None:
            default = reg.default
            le.setText(
                ",".join(map(str, default)) if isinstance(default, tuple) else str(default)
            )
        le.setToolTip(reg.tooltip)
        le.setReadOnly(reg.read_only)
        return le

    # Fallback — never expected, but keep the UI alive if a new kind appears.
    fallback = QLabel(f"<unsupported kind {reg.kind!r}>", parent)
    fallback.setEnabled(False)
    return fallback


class _RegisterPanel(QWidget):
    """One side of the editor: every S-register plus an R-register group."""

    # Emitted whenever the user edits any field in this panel.
    value_changed = Signal(int, bool)  # sreg, is_pin
    # Emitted on focus-in so the parent tab can track which panel is "active".
    interacted = Signal()

    def __init__(self, title: str, is_remote: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._is_remote = is_remote
        self._rows: dict[tuple[int, bool], _Row] = {}
        self._dirty: set[tuple[int, bool]] = set()
        # Per-panel mapping learned from the radio's ATI5 response.
        # Defaults to the canonical SiK layout so a disconnected panel
        # still renders something useful.
        self._sreg_to_name: dict[int, str] = dict(CANONICAL_SIK_NAMES)
        self._pin_sreg_to_name: dict[int, str] = dict(CANONICAL_PIN_NAMES)
        self._name_to_sreg: dict[str, int] = {
            n: s for s, n in self._sreg_to_name.items()
        }
        self._pin_name_to_sreg: dict[str, int] = {
            n: s for s, n in self._pin_sreg_to_name.items()
        }
        self._family: str = FAMILY_SIK
        self._meta: dict[int, ParamMeta] = {}
        # Section header labels (MultiComm) — deleted on every rebuild.
        self._headers: list[QWidget] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        header = QLabel(f"<b>{title}</b>", self)
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(header)

        # Scroll area wrapping the actual register grid + pin group.
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        body = QWidget(scroll)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(4, 4, 4, 4)
        body_layout.setSpacing(2)
        # Stash the layouts so we can clear+rebuild rows when the radio
        # reports a different parameter layout than canonical SiK.
        self._body = body
        self._body_layout = body_layout

        # ---- S-register rows -----------------------------------------
        for reg in all_registers():
            row_widget, row = self._build_row(reg, is_pin=False, parent=body)
            body_layout.addWidget(row_widget)
            self._rows[(reg.sreg, False)] = row

        # ---- Pin map group (collapsible, RFD900x/ux only) ------------
        self._pin_group = QGroupBox("Pin mapping (RFD900x/ux only)", body)
        self._pin_group.setCheckable(True)
        self._pin_group.setChecked(False)
        pin_inner = QWidget(self._pin_group)
        pin_layout = QVBoxLayout(pin_inner)
        pin_layout.setContentsMargins(4, 4, 4, 4)
        pin_layout.setSpacing(2)
        self._pin_inner = pin_inner
        self._pin_layout = pin_layout
        for reg in all_pin_registers():
            row_widget, row = self._build_row(reg, is_pin=True, parent=pin_inner)
            pin_layout.addWidget(row_widget)
            self._rows[(reg.sreg, True)] = row
        group_layout = QVBoxLayout(self._pin_group)
        group_layout.setContentsMargins(6, 6, 6, 6)
        group_layout.addWidget(pin_inner)
        self._pin_inner = pin_inner
        pin_inner.setVisible(False)
        self._pin_group.toggled.connect(pin_inner.setVisible)
        body_layout.addWidget(self._pin_group)

        body_layout.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

    # ---------------------------------------------------------------- helpers
    def _build_row(
        self,
        reg: RegisterDef,
        *,
        is_pin: bool,
        parent: QWidget,
    ) -> tuple[QWidget, _Row]:
        row_widget = QWidget(parent)
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(2, 1, 2, 1)
        row_layout.setSpacing(6)

        prefix = "R" if is_pin else "S"
        label = QLabel(f"<b>{prefix}{reg.sreg}</b> {reg.label}", row_widget)
        label.setToolTip(reg.tooltip)
        label.setMinimumWidth(220)
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        editor = _make_editor(reg, row_widget)
        editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        units = QLabel(reg.units or "", row_widget)
        units.setMinimumWidth(40)
        if reg.kind == "bitmask":
            units.setMinimumWidth(150)

        dirty = QLabel("", row_widget)
        dirty.setStyleSheet("color: #c0392b; font-weight: bold;")
        dirty.setMinimumWidth(12)

        status = QLabel("", row_widget)
        status.setMinimumWidth(16)

        row_layout.addWidget(label)
        row_layout.addWidget(editor, 1)
        row_layout.addWidget(units)
        row_layout.addWidget(dirty)
        row_layout.addWidget(status)

        # Wire up the editor so the parent panel learns about edits and so
        # focus events bubble up as "this panel is active".
        row = _Row(
            reg.sreg, is_pin, reg, row_widget,
            label, editor, dirty, status, units,
        )
        row.refresh_hint()
        self._wire_editor(row)
        # Track interaction by hooking focus events on the editor.
        editor.installEventFilter(_FocusReporter(self))

        return row_widget, row

    def _wire_editor(self, row: _Row) -> None:
        sreg = row.sreg
        is_pin = row.is_pin

        def emit_changed(*_args: object) -> None:
            self._mark_dirty(sreg, is_pin)
            self.value_changed.emit(sreg, is_pin)

        if isinstance(row.editor, QSpinBox):
            row.editor.valueChanged.connect(emit_changed)
        elif isinstance(row.editor, QComboBox):
            row.editor.currentIndexChanged.connect(emit_changed)
        elif isinstance(row.editor, QLineEdit):
            # textEdited fires for user edits only, never for setText().
            row.editor.textEdited.connect(emit_changed)
            row.editor.textEdited.connect(lambda *_a: row.refresh_hint())

    # ---------------------------------------------------------------- public
    def rows(self) -> dict[tuple[int, bool], _Row]:
        return self._rows

    def dirty(self) -> set[tuple[int, bool]]:
        return set(self._dirty)

    def is_remote(self) -> bool:
        return self._is_remote

    def family(self) -> str:
        return self._family

    def meta(self) -> dict[int, ParamMeta]:
        return dict(self._meta)

    def reg_defs(self) -> dict[int, RegisterDef]:
        """sreg → the row's RegisterDef (S-registers only)."""
        return {s: r.reg for (s, p), r in self._rows.items() if not p}

    def s_values(self, *, include_read_only: bool = True) -> dict[int, ParamValue]:
        """sreg → current editor value for every parseable S-register row."""
        out: dict[int, ParamValue] = {}
        for (sreg, is_pin), row in self._rows.items():
            if is_pin or (row.reg.read_only and not include_read_only):
                continue
            v = row.value()
            if v is not None:
                out[sreg] = v
        return out

    def pin_values(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for (sreg, is_pin), row in self._rows.items():
            v = row.value() if is_pin else None
            if isinstance(v, int):
                out[sreg] = v
        return out

    def named_values(self) -> dict[str, ParamValue]:
        """Parameter name → value for writable S-registers (for profiles)."""
        return {
            self._sreg_to_name[s]: v
            for s, v in self.s_values(include_read_only=False).items()
            if s in self._sreg_to_name
        }

    def apply_values(
        self,
        s_params: dict[int, ParamValue],
        pin_params: dict[int, ParamValue] | None = None,
        *,
        s_names: dict[int, str] | None = None,
        pin_names: dict[int, str] | None = None,
        family: str | None = None,
        meta: dict[int, ParamMeta] | None = None,
        keep_dirty: bool = False,
    ) -> None:
        """Update row values from a fresh ATI5 read.

        With ``keep_dirty`` rows holding unsaved user edits keep them — used
        for background re-reads (Compare, Export) so they can't silently
        discard work in progress.

        If ``s_names`` / ``pin_names`` are supplied (typically from
        ``Ati5Result``), the panel rebuilds its rows to match the firmware's
        actual parameter layout — making sure RFDesign 3.x users see
        ``S14: MAX_WINDOW`` instead of the canonical ``S14: RTSCTS``
        labelling.  Existing values are preserved across the rebuild.
        """
        family = family or self._family
        meta = self._meta if meta is None else meta
        if (
            (s_names is not None and s_names != self._sreg_to_name)
            or family != self._family
            or meta != self._meta
        ):
            if pin_names is None:
                # MultiComm has no R-registers; SiK gets the canonical set
                # back even if the previous radio was MultiComm.
                pin_names = (
                    {} if family == FAMILY_MULTICOMM
                    else self._pin_sreg_to_name or dict(CANONICAL_PIN_NAMES)
                )
            self._rebuild_layout(
                s_names=s_names if s_names is not None else self._sreg_to_name,
                pin_names=pin_names,
                family=family,
                meta=meta,
            )
        elif pin_names is not None and pin_names != self._pin_sreg_to_name:
            self._rebuild_layout(
                s_names=self._sreg_to_name,
                pin_names=pin_names,
            )

        for sreg, value in s_params.items():
            row = self._rows.get((sreg, False))
            if row is None and sreg in self._sreg_to_name:
                # Layout already rebuilt above but row missing somehow —
                # add it on the fly. Edge case; shouldn't normally hit.
                continue
            if row is not None:
                if keep_dirty and (sreg, False) in self._dirty:
                    continue
                row.set_value(value)
                self._mark_clean(sreg, False)
        if pin_params:
            for sreg, value in pin_params.items():
                row = self._rows.get((sreg, True))
                if row is not None:
                    if keep_dirty and (sreg, True) in self._dirty:
                        continue
                    row.set_value(value)
                    self._mark_clean(sreg, True)

    # ---------------------------------------------------------------- layout
    def _rebuild_layout(
        self,
        *,
        s_names: dict[int, str],
        pin_names: dict[int, str],
        family: str | None = None,
        meta: dict[int, ParamMeta] | None = None,
    ) -> None:
        """Replace the S/R row widgets with ones reflecting `s_names` /
        `pin_names`.  Preserves dirty state where the sreg still exists in
        the new layout.

        Crucially, deletes only the row containers — the pin_group widget
        (which also lives in body_layout) is left in place so the pin
        rows under it don't get cascade-deleted out from under their dict
        references.
        """
        # Keyed by (name, is_pin), not sreg: the same sreg can mean a
        # different parameter in the new layout (SiK S1 SERIAL_SPEED vs
        # MultiComm S1 BITRATE; canonical vs RFDesign 3.x S13..S15).
        dirty_snapshot: dict[tuple[str, bool], ParamValue] = {}
        for key in list(self._dirty):
            row = self._rows.get(key)
            if row is not None and row.value() is not None:
                dirty_snapshot[(row.reg.name, key[1])] = row.value()  # type: ignore[assignment]
        old_family = self._family

        # Remove only the row containers we created. body_layout's
        # pin_group + stretch stay; pin_layout has only row containers
        # so emptying it via the dict iteration is safe.
        for row in list(self._rows.values()):
            parent_layout = self._pin_layout if row.is_pin else self._body_layout
            parent_layout.removeWidget(row.container)
            row.container.setParent(None)
            row.container.deleteLater()
        for header in self._headers:
            self._body_layout.removeWidget(header)
            header.setParent(None)
            header.deleteLater()
        self._headers.clear()
        self._rows.clear()
        self._dirty.clear()
        if family is not None:
            self._family = family
        if meta is not None:
            self._meta = dict(meta)

        self._sreg_to_name = dict(s_names)
        self._name_to_sreg = {n: s for s, n in self._sreg_to_name.items()}
        self._pin_sreg_to_name = dict(pin_names)
        self._pin_name_to_sreg = {n: s for s, n in self._pin_sreg_to_name.items()}

        # New S-reg rows go BEFORE the pin_group (and stretch) so they
        # render at the top of the panel as expected.
        pin_group_idx = self._body_layout.indexOf(self._pin_group)
        if pin_group_idx < 0:
            pin_group_idx = self._body_layout.count()
        insert_at = pin_group_idx
        defs = [
            derive_def(sreg, name, family=self._family, meta=self._meta.get(sreg))
            for sreg, name in sorted(self._sreg_to_name.items())
        ]
        grouped = self._family == FAMILY_MULTICOMM
        if grouped:
            # Section order, then sreg within a section; unknown last.
            order = {g: i for i, g in enumerate(_MULTICOMM_GROUP_ORDER)}
            defs.sort(key=lambda d: (order.get(d.group, len(order)), d.sreg))
        current_group: str | None = None
        for reg in defs:
            if grouped and reg.group != current_group:
                current_group = reg.group
                header = QLabel(f"<b>{reg.group or 'Other'}</b>", self._body)
                header.setStyleSheet("color: #2c3e50; padding-top: 6px;")
                self._body_layout.insertWidget(insert_at, header)
                self._headers.append(header)
                insert_at += 1
            row_widget, row = self._build_row(reg, is_pin=False, parent=self._body)
            self._body_layout.insertWidget(insert_at, row_widget)
            insert_at += 1
            self._rows[(reg.sreg, False)] = row
        self._pin_group.setVisible(bool(self._pin_sreg_to_name))

        for sreg in sorted(self._pin_sreg_to_name.keys()):
            name = self._pin_sreg_to_name[sreg]
            reg = derive_def(sreg, name)
            reg = RegisterDef(
                sreg=reg.sreg, name=reg.name,
                label=f"Pin R{sreg} {name}" if name != "PIN_FUNC" else f"Pin R{sreg} function",
                tooltip=reg.tooltip, kind=reg.kind,
                minimum=reg.minimum, maximum=reg.maximum, enum=reg.enum,
                default=reg.default, units=reg.units,
                variant_notes=reg.variant_notes, read_only=reg.read_only,
            )
            row_widget, row = self._build_row(reg, is_pin=True, parent=self._pin_inner)
            self._pin_layout.addWidget(row_widget)
            self._rows[(sreg, True)] = row

        if self._family != old_family:
            return   # names from another firmware family don't carry over
        by_name = {(r.reg.name, p): (s, p) for (s, p), r in self._rows.items()}
        for name_key, value in dirty_snapshot.items():
            key = by_name.get(name_key)
            if key is not None:
                self._rows[key].set_value(value)
                self._mark_dirty(*key)

    def sreg_to_name(self) -> dict[int, str]:
        return dict(self._sreg_to_name)

    def name_to_sreg(self) -> dict[str, int]:
        return dict(self._name_to_sreg)

    def mark_all_clean(self) -> None:
        for key in list(self._dirty):
            self._mark_clean(key[0], key[1])

    def mark_dirty(self, sreg: int, is_pin: bool) -> None:
        self._mark_dirty(sreg, is_pin)

    def mark_clean(self, sreg: int, is_pin: bool) -> None:
        self._mark_clean(sreg, is_pin)

    def restore_defaults(self) -> None:
        for (sreg, is_pin), row in self._rows.items():
            if row.reg.read_only:
                continue
            if row.reg.default is None:
                continue
            row.set_value(row.reg.default)
            self._mark_dirty(sreg, is_pin)

    def get_value(self, sreg: int, is_pin: bool = False) -> ParamValue | None:
        row = self._rows.get((sreg, is_pin))
        return None if row is None else row.value()

    def set_value(self, sreg: int, value: ParamValue, is_pin: bool = False) -> None:
        row = self._rows.get((sreg, is_pin))
        if row is None:
            return
        row.set_value(value)
        self._mark_dirty(sreg, is_pin)

    # ---------------------------------------------------------------- private
    def _mark_dirty(self, sreg: int, is_pin: bool) -> None:
        key = (sreg, is_pin)
        row = self._rows.get(key)
        if row is None:
            return
        self._dirty.add(key)
        row.is_dirty = True
        row.apply_visual()

    def _mark_clean(self, sreg: int, is_pin: bool) -> None:
        key = (sreg, is_pin)
        self._dirty.discard(key)
        row = self._rows.get(key)
        if row is None:
            return
        row.is_dirty = False
        row.apply_visual()

    # ---------------------------------------------------------------- validation
    def apply_validation(self, report: ValidationReport) -> None:
        """Tint each row according to the worst issue affecting it.

        Rows with no issues become "ok" green for a few seconds (the parent
        tab can call clear_validation() on a timer to fade them).
        """
        # Worst severity per sreg, with rank order error > warning > info > ok
        rank = {"error": 3, "warning": 2, "info": 1, "ok": 0, "": -1}
        per_sreg = report.issues_by_sreg
        for (sreg, is_pin), row in self._rows.items():
            if is_pin:
                # Pin registers aren't covered by current validation rules.
                row.validation_severity = ""
                row.validation_titles = ()
                row.apply_visual()
                continue
            issues = per_sreg.get(sreg, [])
            if not issues:
                row.validation_severity = "ok"
                row.validation_titles = ()
            else:
                worst = max(issues, key=lambda i: rank.get(i.severity, 0))
                row.validation_severity = worst.severity
                row.validation_titles = tuple(i.title for i in issues)
            row.apply_visual()

    def clear_validation(self) -> None:
        for row in self._rows.values():
            row.validation_severity = ""
            row.validation_titles = ()
            row.apply_visual()


_MULTICOMM_GROUP_ORDER: tuple[str, ...] = (
    multicomm.GROUP_LINK,
    multicomm.GROUP_NETWORK,
    multicomm.GROUP_ROUTING,
    multicomm.GROUP_ENCRYPTION,
    multicomm.GROUP_SERIAL1,
    multicomm.GROUP_SERIAL2,
    multicomm.GROUP_GPIO,
    multicomm.GROUP_RC,
    multicomm.GROUP_MISC,
)


class _FocusReporter(QWidget):
    """Tiny event filter that re-emits focus-in as the panel's `interacted`."""

    def __init__(self, panel: "_RegisterPanel") -> None:
        super().__init__(panel)
        self._panel = panel

    def eventFilter(self, _obj: object, event: object) -> bool:  # type: ignore[override]
        # Qt's FocusIn event type number; importing QEvent here keeps the
        # public surface tidy.
        from PySide6.QtCore import QEvent

        if isinstance(event, QEvent) and event.type() == QEvent.Type.FocusIn:
            self._panel.interacted.emit()
        return False


class SettingsTab(QWidget):
    """Two-panel S-register editor for local + remote radios."""

    status_message = Signal(str, int)  # text, timeout_ms

    def __init__(self, radio: Radio, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._radio = radio
        self._state: str = Radio.STATE_DISCONNECTED

        # Sequenced "Load Settings" state: after the local read succeeds we
        # fire the remote read in the params_loaded handler.
        self._load_phase: Optional[str] = None  # None | "local" | "remote"

        # Multi-write tracking for "Save Settings".
        self._save_pending: set[tuple[int, bool, bool]] = set()  # (sreg, is_remote, is_pin)
        self._save_failures: int = 0
        self._save_rejected: list[tuple[str, int, int]] = []  # (label, sreg, value) for regs the radio said ERROR to

        # Sides whose next params_loaded comes from a fresh connection and
        # should overwrite (not merge with) unsaved edits.
        self._overwrite_next: set[bool] = set()

        # EEPROM save tracking.
        self._eeprom_pending: set[bool] = set()  # set of is_remote flags

        # Track the panel the user last interacted with — drives "Restore Defaults".
        self._active_panel: Optional[_RegisterPanel] = None

        # Cached board name + ATI banner from radio_info.  Both fed into
        # validate_config: board_name powers model-specific rules, banner
        # powers firmware-lockdown detection (R19).
        self._board_name: str = ""
        self._firmware_banner: str = ""
        self._family: str = FAMILY_SIK
        # The radio's last-known values.  Populated by params_loaded events;
        # R19 needs this to detect "user wants X but radio is locked at Y".
        self._radio_current_values: dict[int, ParamValue] = {}

        self._build_ui()
        self._wire_radio()
        self._set_buttons_enabled(False)

    # ---------------------------------------------------------------- UI build
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # ---- action button row ---------------------------------------
        actions = QHBoxLayout()
        actions.setSpacing(6)

        self._btn_load = QPushButton("Load Settings", self)
        self._btn_save = QPushButton("Save Settings", self)
        self._btn_validate = QPushButton("Validate", self)
        self._btn_copy = QPushButton("Copy → Remote", self)
        self._btn_restore = QPushButton("Restore Defaults", self)
        self._btn_eeprom = QPushButton("Save EEPROM", self)
        self._btn_reboot = QPushButton("Reboot", self)
        self._btn_factory = QPushButton("Factory Reset", self)

        self._btn_profiles = QToolButton(self)
        self._btn_profiles.setText("Profiles")
        self._btn_profiles.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._profiles_menu = QMenu(self._btn_profiles)
        # Rebuilt every time the menu is shown so user-preset additions and
        # board-detection updates are always reflected.
        self._profiles_menu.aboutToShow.connect(self._populate_profiles_menu)
        self._populate_profiles_menu()
        self._btn_profiles.setMenu(self._profiles_menu)

        # MultiComm: which NODEID RT commands (remote panel) talk to.
        self._node_label = QLabel("Remote node:", self)
        self._node_spin = QSpinBox(self)
        self._node_spin.setRange(-1, multicomm.MAX_NODEID)
        self._node_spin.setSpecialValueText("all (broadcast)")
        self._node_spin.setValue(-1)
        self._node_spin.setToolTip(
            "MultiComm sends RT commands to every node in range unless one is "
            "addressed (RTI5@N). Pick the NODEID the REMOTE panel should read "
            "and write. 'all' is fine for a point-to-point pair, but in a "
            "network every node would answer at once."
        )
        self._node_spin.valueChanged.connect(self._on_remote_node_changed)

        # Compact summary label that shows the last validation tally.
        self._validation_status = QLabel("", self)
        self._validation_status.setStyleSheet("color: #555;")

        for btn in (
            self._btn_load,
            self._btn_save,
            self._btn_validate,
            self._btn_copy,
            self._btn_restore,
            self._btn_eeprom,
            self._btn_reboot,
            self._btn_factory,
        ):
            actions.addWidget(btn)
        actions.addWidget(self._btn_profiles)
        actions.addWidget(self._node_label)
        actions.addWidget(self._node_spin)
        actions.addWidget(self._validation_status)
        actions.addStretch(1)
        root.addLayout(actions)

        # ---- two register panels -------------------------------------
        panels = QHBoxLayout()
        panels.setSpacing(6)
        self._local_panel = _RegisterPanel("LOCAL", is_remote=False, parent=self)
        self._remote_panel = _RegisterPanel("REMOTE", is_remote=True, parent=self)
        panels.addWidget(self._local_panel, 1)
        panels.addWidget(self._remote_panel, 1)
        root.addLayout(panels, 1)

        # ---- status line ---------------------------------------------
        self._status = QLabel("", self)
        self._status.setStyleSheet("color: #555;")
        root.addWidget(self._status)

        # ---- panel interaction tracking ------------------------------
        self._local_panel.interacted.connect(lambda: self._set_active_panel(self._local_panel))
        self._remote_panel.interacted.connect(lambda: self._set_active_panel(self._remote_panel))
        self._active_panel = self._local_panel

        # ---- button wiring -------------------------------------------
        self._btn_load.clicked.connect(self._on_load_clicked)
        self._btn_save.clicked.connect(self._on_save_clicked)
        self._btn_validate.clicked.connect(self._on_validate_clicked)
        self._btn_copy.clicked.connect(self._on_copy_to_remote_clicked)
        self._btn_restore.clicked.connect(self._on_restore_defaults_clicked)
        self._btn_eeprom.clicked.connect(self._on_save_eeprom_clicked)
        self._btn_reboot.clicked.connect(self._on_reboot_clicked)
        self._btn_factory.clicked.connect(self._on_factory_reset_clicked)
        self._apply_family_ui()

    def _apply_family_ui(self) -> None:
        """Show/hide controls that only make sense for one firmware family."""
        is_mc = self._family == FAMILY_MULTICOMM
        self._node_label.setVisible(is_mc)
        self._node_spin.setVisible(is_mc)
        if not is_mc and self._node_spin.value() != -1:
            # A hidden, stale node selection must not follow us to SiK.
            self._node_spin.setValue(-1)
        self._btn_eeprom.setToolTip(
            "MultiComm saves every parameter as it is written — there is no "
            "AT&W. Reboot the radios to apply changes."
            if is_mc else
            "Write the current settings to EEPROM (AT&W) on local and remote."
        )

    def _on_remote_node_changed(self, value: int) -> None:
        node = None if value < 0 else value
        self._radio.set_remote_node(node)
        # The REMOTE panel shows the previous node's values; drop its
        # unsaved edits and grey it out until the new node is loaded, so
        # one node's settings can't be saved onto another.
        self._remote_panel.mark_all_clean()
        self._remote_panel.setEnabled(False)
        self._emit_status(
            "Remote panel addresses all nodes (broadcast)." if node is None
            else f"Remote panel addresses NODEID {node} (RT…@{node}). "
                 "Click Load Settings to read it.",
            5000,
        )

    def _populate_profiles_menu(self) -> None:
        """(Re)build the Profiles dropdown.

        Called once at init and again every time the menu is about to show,
        so user-preset additions and board-detection updates appear without
        the user having to restart the app.
        """
        self._profiles_menu.clear()

        sik_layout = (
            (CATEGORY_REGION,    "Region & frequency"),
            (CATEGORY_USE_CASE,  "Use case"),
            (CATEGORY_MODEL,     "Model defaults"),
            (CATEGORY_MAXIMIZE,  "Maximize (combos)"),
        )
        mc_layout = ((CATEGORY_MULTICOMM, "MultiComm"),)
        # Presets for the connected firmware first.
        category_layout = (
            mc_layout + sik_layout if self._family == FAMILY_MULTICOMM
            else sik_layout + mc_layout
        )
        for category, label in category_layout:
            sub = self._profiles_menu.addMenu(label)
            for preset in presets_by_category(category):
                self._add_preset_action(sub, preset)

        self._profiles_menu.addSeparator()

        # User presets — refreshed on every menu show.
        user_sub = self._profiles_menu.addMenu("User presets")
        user_presets = list_user_profiles()
        if not user_presets:
            empty = QAction("(no saved presets yet)", user_sub)
            empty.setEnabled(False)
            user_sub.addAction(empty)
        else:
            for preset in user_presets:
                self._add_preset_action(user_sub, preset, is_user=True)
            user_sub.addSeparator()
            manage = QAction("Manage user presets…", user_sub)
            manage.triggered.connect(self._on_manage_user_presets)
            user_sub.addAction(manage)

        save_user_act = QAction("Save current as user preset…", self._profiles_menu)
        save_user_act.triggered.connect(self._on_save_user_preset)
        self._profiles_menu.addAction(save_user_act)

        export_live_act = QAction("Export live config from radio…", self._profiles_menu)
        export_live_act.setToolTip(
            "Re-read the radio's current settings via ATI5 and save them as a "
            "transferable JSON profile. Use this to copy a working config to "
            "another PC."
        )
        export_live_act.triggered.connect(self._on_export_live_config)
        export_live_act.setEnabled(self._state in _LIVE_STATES)
        self._profiles_menu.addAction(export_live_act)

        # Compare submenu: live vs JSON / live vs remote
        compare_sub = self._profiles_menu.addMenu("Compare configs")
        compare_json_act = QAction("…with a JSON profile…", compare_sub)
        compare_json_act.triggered.connect(self._on_compare_with_json)
        compare_json_act.setEnabled(self._state in _LIVE_STATES)
        compare_sub.addAction(compare_json_act)
        compare_remote_act = QAction("…with the remote radio (live)", compare_sub)
        compare_remote_act.triggered.connect(self._on_compare_with_remote)
        compare_remote_act.setEnabled(self._state in _LIVE_STATES)
        compare_sub.addAction(compare_remote_act)

        self._profiles_menu.addSeparator()

        load_act = QAction("Import preset from JSON…", self._profiles_menu)
        load_act.triggered.connect(self._on_profile_load)
        self._profiles_menu.addAction(load_act)

        save_act = QAction("Export current panel to JSON…", self._profiles_menu)
        save_act.triggered.connect(self._on_profile_save)
        self._profiles_menu.addAction(save_act)

    def _add_preset_action(
        self,
        menu: QMenu,
        preset: Profile,
        *,
        is_user: bool = False,
    ) -> None:
        board_ok = preset.matches_board(self._board_name)
        fw_ok = preset.matches_firmware(self._family)
        applies = board_ok and fw_ok
        target = self._board_name if not board_ok else (
            "MultiComm" if self._family == FAMILY_MULTICOMM else "SiK"
        )
        text = preset.name if applies else f"{preset.name}  (n/a for {target})"
        act = QAction(text, menu)
        tip = preset.description
        if not applies:
            tip += f"  · This preset isn't meant for {target} radios."
        if preset.notes:
            tip += f"\n\n{preset.notes}"
        act.setToolTip(tip)
        if not applies:
            font = act.font()
            font.setItalic(True)
            act.setFont(font)
        # default arg pins the current preset by value
        act.triggered.connect(lambda _checked=False, p=preset: self._apply_profile(p))
        menu.addAction(act)

    # ---------------------------------------------------------------- radio wiring
    def _wire_radio(self) -> None:
        self._radio.state_changed.connect(self._on_state_changed)
        self._radio.params_loaded.connect(self._on_params_loaded)
        self._radio.params_failed.connect(self._on_params_failed)
        self._radio.write_result.connect(self._on_write_result)
        self._radio.eeprom_saved.connect(self._on_eeprom_saved)
        self._radio.factory_reset_done.connect(self._on_factory_reset_done)
        self._radio.error.connect(self._on_radio_error)
        # Track the connected radio's board name so presets can be filtered
        # by applies_to and validation can apply model-specific rules.
        self._radio.radio_info.connect(self._on_radio_info)

    def _on_radio_info(self, info: object) -> None:
        try:
            self._board_name = str(info.get("board_name") or "")  # type: ignore[union-attr]
            self._firmware_banner = str(info.get("banner") or "")  # type: ignore[union-attr]
            self._set_family(str(info.get("firmware_family") or FAMILY_SIK))  # type: ignore[union-attr]
        except Exception:
            self._board_name = ""
            self._firmware_banner = ""

    def _set_family(self, family: str) -> None:
        if family != self._family:
            self._family = family
            self._apply_family_ui()

    # ---------------------------------------------------------------- state handling
    def _on_state_changed(self, state: str) -> None:
        if self._state == Radio.STATE_DISCONNECTED and state != Radio.STATE_DISCONNECTED:
            # New connection — possibly a different radio.  The connect-time
            # reads replace whatever was staged for the previous one.
            self._overwrite_next = {False, True}
            self._remote_panel.setEnabled(True)
        self._state = state
        self._set_buttons_enabled(state in _LIVE_STATES)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for btn in (
            self._btn_load,
            self._btn_save,
            self._btn_copy,
            self._btn_restore,
            self._btn_eeprom,
            self._btn_reboot,
            self._btn_factory,
            self._btn_profiles,
        ):
            btn.setEnabled(enabled)
        # Validate is intentionally available even while disconnected — users
        # may want to sanity-check a JSON profile they just loaded before
        # connecting to a radio.
        self._btn_validate.setEnabled(True)

    def _set_active_panel(self, panel: _RegisterPanel) -> None:
        self._active_panel = panel

    def _emit_status(self, text: str, timeout_ms: int = 5000) -> None:
        self._status.setText(text)
        self.status_message.emit(text, timeout_ms)

    # ---------------------------------------------------------------- actions
    def _on_load_clicked(self) -> None:
        # Sequence: read local, then on success read remote (driven by the
        # params_loaded handler — see _on_params_loaded).
        self._load_phase = "local"
        self._emit_status("Loading local settings…", 3000)
        self._radio.read_params(False)

    def _on_save_clicked(self) -> None:
        self._save_pending.clear()
        self._save_failures = 0
        self._save_rejected.clear()
        skipped: list[str] = []
        # Batch local and remote writes into one round-trip each — staying in
        # command mode for the whole set is dramatically faster than doing
        # one +++/ATO bracket per register, and also avoids serial races.
        local_batch: list[tuple[int, ParamValue, bool, bool]] = []
        remote_batch: list[tuple[int, ParamValue, bool, bool]] = []

        for panel in (self._local_panel, self._remote_panel):
            is_remote = panel.is_remote()
            for sreg, is_pin in sorted(panel.dirty()):
                row = panel.rows()[(sreg, is_pin)]
                value = row.value()
                if value is None:
                    skipped.append(f"S{sreg} {row.reg.name}: not a valid value")
                    continue
                # Validate against this row's own definition — correct for
                # any firmware layout (canonical SiK sregs are not).
                ok, reason = validate_def(row.reg, value)
                if not ok:
                    skipped.append(reason)
                    continue
                self._save_pending.add((sreg, is_remote, is_pin))
                (remote_batch if is_remote else local_batch).append(
                    (sreg, value, is_remote, is_pin)
                )

        if skipped:
            for reason in skipped:
                self._emit_status(reason, 6000)

        remote_batch = self._guard_broadcast_writes(remote_batch)

        attempted = len(local_batch) + len(remote_batch)
        if attempted == 0:
            if not skipped:
                self._emit_status("No changes to save.", 4000)
            return

        if local_batch:
            self._radio.write_params_batch(local_batch)
        if remote_batch:
            self._radio.write_params_batch(remote_batch)
        self._emit_status(f"Saving {attempted} register(s)…", 3000)

    def _guard_broadcast_writes(
        self, remote_batch: list[tuple[int, ParamValue, bool, bool]],
    ) -> list[tuple[int, ParamValue, bool, bool]]:
        """MultiComm with no node selected broadcasts RT writes to every
        node.  Per-node settings (NODEID, SLOTIDS, DESTID…) written that way
        would make every node identical — confirm first, else drop them."""
        if self._family != FAMILY_MULTICOMM or self._radio.remote_node is not None:
            return remote_batch
        rows = self._remote_panel.rows()
        per_node = [
            u for u in remote_batch
            if not u[3] and rows.get((u[0], False)) is not None
            and rows[(u[0], False)].reg.scope != "network"
        ]
        if not per_node:
            return remote_batch
        names = ", ".join(rows[(u[0], False)].reg.name for u in per_node)
        ans = QMessageBox.warning(
            self,
            "Broadcast per-node settings?",
            f"No remote node is selected, so these per-node settings would be "
            f"written to EVERY node in range: {names}.\n\n"
            "Select a node (Remote node: …) to write just one. Send them to "
            "all nodes anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            return remote_batch
        for u in per_node:
            self._save_pending.discard((u[0], True, u[3]))
        return [u for u in remote_batch if u not in per_node]

    def _on_copy_to_remote_clicked(self) -> None:
        # MultiComm: copy only "same for all nodes" settings — per-node ones
        # (NODEID, SLOTIDS, DESTID…) must stay different on each node.
        network_only = self._family == FAMILY_MULTICOMM
        copied = 0
        remote_rows = self._remote_panel.rows()
        for (sreg, is_pin), row in sorted(self._local_panel.rows().items()):
            if is_pin or row.reg.read_only:
                continue
            if network_only and row.reg.scope != "network":
                continue
            local_val = row.value()
            remote_row = remote_rows.get((sreg, False))
            if local_val is None or remote_row is None or remote_row.reg.name != row.reg.name:
                continue
            if remote_row.value() == local_val:
                continue
            self._remote_panel.set_value(sreg, local_val, is_pin=False)
            copied += 1
        scope = " network-wide" if network_only else ""
        if copied:
            self._emit_status(
                f"Copied {copied}{scope} register(s) to remote panel (unsaved).", 5000,
            )
        else:
            self._emit_status(f"Remote already matches local{scope} settings.", 4000)

    def _on_restore_defaults_clicked(self) -> None:
        panel = self._active_panel or self._local_panel
        panel.restore_defaults()
        side = "remote" if panel.is_remote() else "local"
        self._emit_status(f"Restored defaults on {side} panel (unsaved).", 5000)

    def _on_save_eeprom_clicked(self) -> None:
        if self._family == FAMILY_MULTICOMM:
            QMessageBox.information(
                self, "Save to EEPROM",
                "MultiComm firmware saves each parameter as soon as it is "
                "written (there is no AT&W). Use Reboot to make the new "
                "settings take effect.",
            )
            return
        ans = QMessageBox.question(
            self,
            "Save to EEPROM",
            "Save current settings to EEPROM (AT&W) on local AND remote? "
            "This is permanent until next AT&F.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._eeprom_pending = {False, True}
        self._emit_status("Saving EEPROM on local + remote…", 3000)
        self._radio.save_eeprom(False)
        self._radio.save_eeprom(True)

    def _on_reboot_clicked(self) -> None:
        ans = QMessageBox.question(
            self,
            "Reboot local radio",
            "Reboot the LOCAL radio now? The link will drop momentarily.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._radio.reboot(False)
        self._emit_status("Rebooting local radio…", 3000)

        node = self._radio.remote_node
        remote_desc = (
            "EVERY remote node in range (no node selected)"
            if self._family == FAMILY_MULTICOMM and node is None
            else f"remote NODEID {node}" if self._family == FAMILY_MULTICOMM
            else "the REMOTE radio"
        )
        ans2 = QMessageBox.question(
            self,
            "Reboot remote radio",
            f"Also reboot {remote_desc}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans2 == QMessageBox.StandardButton.Yes:
            self._radio.reboot(True)
            self._emit_status("Rebooting remote radio…", 3000)

    def _on_factory_reset_clicked(self) -> None:
        ans = QMessageBox.warning(
            self,
            "Factory reset",
            "This erases ALL settings (AT&F) — frequencies, NETID, baud — "
            "you may lose connection until you reconfigure. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._radio.factory_reset(False)
        self._emit_status("Factory reset issued to local radio…", 4000)

    # ---------------------------------------------------------------- profile actions
    def _apply_profile(self, profile: Profile) -> None:
        """Show the confirm-and-diff dialog, then stage values on the local panel."""
        dialog = ApplyPresetDialog(
            profile,
            self._local_panel.s_values(),
            board_name=self._board_name,
            name_to_sreg=self._local_panel.name_to_sreg(),
            sreg_to_name=self._local_panel.sreg_to_name(),
            reg_defs=self._local_panel.reg_defs(),
            family=self._family,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._apply_profile_force(profile)

    def _apply_profile_force(self, profile: Profile) -> None:
        # Translate the preset's name-keyed params via the panel's actual
        # firmware mapping. Names absent on this radio (e.g. MANCHESTER on
        # RFDesign 3.x) are silently skipped — the underlying parameter
        # doesn't exist on the connected hardware.
        name_to_sreg = self._local_panel.name_to_sreg()
        target_sregs = profile.to_sregs_for(name_to_sreg)
        applied = 0
        skipped_names = []
        for n in sorted(profile.params):
            if n not in name_to_sreg:
                skipped_names.append(n)
        for sreg, value in target_sregs.items():
            row = self._local_panel.rows().get((int(sreg), False))
            if row is None or row.reg.read_only:
                continue
            self._local_panel.set_value(int(sreg), coerce_value(value), is_pin=False)
            applied += 1
        for sreg, value in profile.pin_registers.items():
            row = self._local_panel.rows().get((int(sreg), True))
            if row is not None:
                self._local_panel.set_value(int(sreg), int(value), is_pin=True)
                applied += 1
        msg = (
            f"Applied preset '{profile.name}' to local panel "
            f"({applied} registers, unsaved). Click Save Settings to commit."
        )
        if skipped_names:
            msg += (
                f"  Skipped {len(skipped_names)} parameter(s) not present on "
                f"this radio: {', '.join(skipped_names)}."
            )
        self._emit_status(msg, 8000)

    # ---------------------------------------------------------------- validation
    def _on_validate_clicked(self) -> None:
        report = validate_config(
            self._local_panel.s_values(),
            sreg_to_name=self._local_panel.sreg_to_name(),
            pin_params=self._local_panel.pin_values(),
            board_name=self._board_name,
            is_remote=False,
            firmware_banner=self._firmware_banner,
            current_values=self._radio_current_values,
            family=self._local_panel.family(),
            meta=self._local_panel.meta(),
        )
        self._local_panel.apply_validation(report)

        # Compact tally next to the action bar.
        if report.overall == "ok":
            self._validation_status.setText(
                "<span style='color:#27ae60;'>✓ all rules pass</span>"
            )
        else:
            parts: list[str] = []
            if report.errors:
                parts.append(f"<span style='color:#c0392b;'>✕ {len(report.errors)}</span>")
            if report.warnings:
                parts.append(f"<span style='color:#d68910;'>⚠ {len(report.warnings)}</span>")
            if report.infos:
                parts.append(f"<span style='color:#2874a6;'>ⓘ {len(report.infos)}</span>")
            self._validation_status.setText(" ".join(parts))
        self._validation_status.setTextFormat(Qt.TextFormat.RichText)

        # Open summary dialog. Hook the "Apply fix" buttons.
        dialog = ValidationDialog(report, reg_defs=self._local_panel.reg_defs(), parent=self)
        dialog.apply_fix_requested.connect(self._on_apply_validation_fix)
        dialog.exec()

    def _on_apply_validation_fix(self, sreg: int, value: object) -> None:
        value = coerce_value(value)
        self._local_panel.set_value(sreg, value, is_pin=False)
        # Mark dirty so the user notices. Validation tints stay until next click.
        self._local_panel.mark_dirty(sreg, is_pin=False)
        self._emit_status(f"Staged fix: S{sreg} = {value} (unsaved)", 5000)

    # ---------------------------------------------------------------- user preset save / manage
    def _on_save_user_preset(self) -> None:
        dialog = SavePresetDialog(
            board_name=self._board_name,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = dialog.name()
        if not name:
            QMessageBox.warning(self, "Save preset", "Name cannot be empty.")
            return

        # Name-keyed from the panel's actual layout, so non-canonical
        # firmwares (SiK 3.x, MultiComm) round-trip correctly.
        profile = Profile(
            name=name,
            description=dialog.description(),
            params=self._local_panel.named_values(),
            pin_registers=self._local_panel.pin_values(),
            applies_to=dialog.applies_to(),
            category="user",
            notes=dialog.notes(),
            firmware=self._local_panel.family(),
        )
        try:
            target = save_user_profile(profile)
        except Exception as e:
            QMessageBox.critical(self, "Save preset failed", str(e))
            return
        self._emit_status(f"Saved preset '{name}' to {target}", 5000)

    # ---------------------------------------------------------------- export live config
    def _on_export_live_config(self) -> None:
        """Re-read ATI5 from the radio and save the result as a Profile.

        Listens for the next ``params_loaded(local)`` signal, builds a
        :class:`Profile` from the firmware's reported names + radio_info,
        and prompts for a save location (user preset dir + optional JSON).
        """
        if self._state not in _LIVE_STATES:
            QMessageBox.warning(
                self, "Export live config",
                "Connect to a radio first.",
            )
            return

        # Cache the radio_info for the capture (banner + board name).
        radio_info = {
            "banner": self._firmware_banner,
            "board_name": self._board_name,
        }

        self._emit_status("Re-reading live config from radio…", 3000)
        self._radio.read_params_once(
            False,
            lambda result: self._show_export_dialog(result, radio_info),
            lambda reason: self._emit_status(f"Export failed: {reason}", 8000),
        )

    def _show_export_dialog(self, ati5: object, radio_info: dict) -> None:
        # Pick a default name based on board + timestamp.
        from datetime import datetime
        board = radio_info.get("board_name") or "rfd900"
        default_name = f"{board} live {datetime.now().strftime('%Y-%m-%d %H:%M')}"

        # Collect name + description + notes via the existing SavePresetDialog
        # so the UX matches "Save current as user preset…".
        from .validation_dialog import SavePresetDialog
        dialog = SavePresetDialog(
            suggested_name=default_name,
            board_name=self._board_name,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = dialog.name()
        if not name:
            QMessageBox.warning(self, "Export live config", "Name cannot be empty.")
            return

        profile = profile_from_ati5(
            name,
            ati5,
            radio_info=radio_info,
            description=dialog.description(),
            notes=dialog.notes(),
        )
        # If the user opted to tag this preset as board-specific, override
        # what profile_from_ati5 stamped (the dialog's choice wins).
        if dialog.applies_to():
            profile.applies_to = dialog.applies_to()

        # Save to user preset dir.
        try:
            target = save_user_profile(profile)
        except Exception as e:
            QMessageBox.critical(self, "Export live config failed", str(e))
            return

        # Offer to also write a portable copy for cross-PC transfer.
        ans = QMessageBox.question(
            self, "Export live config",
            (
                f"Saved live config to:\n  {target}\n\n"
                f"Captured {len(profile.params)} parameter(s) from {board}.\n\n"
                "Also save a portable JSON copy somewhere else "
                "(USB stick / shared folder)?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            path, _ = QFileDialog.getSaveFileName(
                self, "Save portable copy", f"{name}.json",
                "JSON profiles (*.json);;All files (*)",
            )
            if path:
                try:
                    save_profile(path, profile)
                except Exception as e:
                    QMessageBox.critical(self, "Save copy failed", str(e))
                    return
                self._emit_status(f"Saved portable copy to {path}", 6000)

        self._emit_status(
            f"Exported live config '{name}' "
            f"({len(profile.params)} parameters)",
            6000,
        )

    # ---------------------------------------------------------------- compare
    def _on_compare_with_json(self) -> None:
        """Diff the local radio's live config against a saved JSON profile."""
        if self._state not in _LIVE_STATES:
            QMessageBox.warning(
                self, "Compare configs", "Connect to a radio first.",
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Compare against profile",
            "",
            "JSON profiles (*.json);;All files (*)",
        )
        if not path:
            return
        try:
            ref = load_profile(path)
        except Exception as e:
            QMessageBox.critical(self, "Load failed", str(e))
            return

        # Re-read local via ATI5 to capture firmware names + values
        def _on_loaded(result):
            self._show_compare_dialog(
                "Local (this radio)",
                getattr(result, "s_params", {}) or {},
                getattr(result, "s_names", {}) or {},
                f"Profile: {ref.name}",
                ref.params,
                {},   # JSON profile doesn't carry sregs — ok, dialog tolerates
                peer=False,
            )
        self._emit_status("Re-reading live config to compare…", 3000)
        self._radio.read_params_once(
            False, _on_loaded,
            lambda reason: self._emit_status(f"Compare failed: {reason}", 8000),
        )

    def _on_compare_with_remote(self) -> None:
        """Diff the local radio's live config against the remote (RTI5)."""
        if self._state not in _LIVE_STATES:
            QMessageBox.warning(
                self, "Compare configs", "Connect to a radio first.",
            )
            return

        local_payload = {"params": {}, "names": {}}
        remote_payload = {"params": {}, "names": {}}

        # Capture local first
        def _on_local(result):
            local_payload["params"] = getattr(result, "s_params", {}) or {}
            local_payload["names"] = getattr(result, "s_names", {}) or {}
            # Now request remote
            self._emit_status("Reading remote settings (RTI5)…", 4000)
            self._radio.read_params_once(True, _on_remote, _on_remote_error)

        def _on_remote(result):
            remote_payload["params"] = getattr(result, "s_params", {}) or {}
            remote_payload["names"] = getattr(result, "s_names", {}) or {}
            _show()

        def _on_remote_error(_msg):
            QMessageBox.warning(
                self, "Remote unreachable",
                "The remote radio didn't answer RTI5 — the radios aren't "
                "linked yet (or, on MultiComm, the selected remote node isn't "
                "in range), so its config can't be read. Pair them first, "
                "or use 'Compare with JSON profile' instead.",
            )

        def _show():
            self._show_compare_dialog(
                "Local",
                local_payload["params"], local_payload["names"],
                "Remote (live)",
                remote_payload["params"], remote_payload["names"],
                peer=True,
            )

        self._emit_status("Reading local settings…", 3000)
        self._radio.read_params_once(
            False, _on_local,
            lambda reason: self._emit_status(f"Compare failed: {reason}", 8000),
        )

    def _show_compare_dialog(
        self,
        a_label: str,
        a_s_params: dict[int, ParamValue],
        a_s_names: dict[int, str],
        b_label: str,
        b_params_by_name_or_sreg,
        b_s_names: dict[int, str],
        *,
        peer: bool = False,
    ) -> None:
        from .compare_dialog import CompareConfigsDialog
        # A side: build name → value and name → sreg from ATI5 result
        a_params: dict[str, ParamValue] = {}
        a_sregs: dict[str, int] = {}
        for sreg, name in a_s_names.items():
            if not name:
                continue
            a_sregs[name] = sreg
            if sreg in a_s_params:
                a_params[name] = a_s_params[sreg]
        # B side: tolerate either {sreg: val} (with names) or {name: val}
        b_params: dict[str, ParamValue] = {}
        b_sregs: dict[str, int] = {}
        if b_s_names:
            # remote ATI5
            for sreg, name in b_s_names.items():
                if not name:
                    continue
                b_sregs[name] = sreg
                if sreg in b_params_by_name_or_sreg:
                    b_params[name] = b_params_by_name_or_sreg[sreg]
        else:
            # name-keyed (e.g. profile.params)
            for k, v in b_params_by_name_or_sreg.items():
                b_params[str(k)] = coerce_value(v)
            # No B-side sregs available; CompareConfigsDialog renders ok.
        dialog = CompareConfigsDialog(
            a_label, a_params, a_sregs,
            b_label, b_params, b_sregs,
            family=self._local_panel.family(),
            peer=peer,
            parent=self,
        )
        dialog.apply_fix_requested.connect(self._on_apply_validation_fix)
        dialog.apply_all_fixes_requested.connect(self._on_apply_bulk_fixes)
        dialog.exec()

    def _on_apply_bulk_fixes(self, bundle) -> None:
        """Stage a set of (sreg, value) fixes from the Compare dialog."""
        for sreg, value in bundle:
            self._on_apply_validation_fix(int(sreg), value)

    def _on_manage_user_presets(self) -> None:
        # Minimal manage flow: list + delete via QMessageBox confirms.
        # A richer dialog can replace this later without affecting the API.
        presets = list_user_profiles()
        if not presets:
            QMessageBox.information(self, "User presets", "No saved presets yet.")
            return
        names = "\n".join(f"  • {p.name}" for p in presets)
        msg = QMessageBox(self)
        msg.setWindowTitle("User presets")
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText(
            f"Saved presets in {user_profile_dir()}:\n\n{names}\n\n"
            "Edit or delete files directly in that folder, or use the dialog "
            "below to remove one."
        )
        msg.setStandardButtons(QMessageBox.StandardButton.Close)
        delete_btn = msg.addButton("Delete a preset…", QMessageBox.ButtonRole.ActionRole)
        msg.exec()
        if msg.clickedButton() is delete_btn:
            choices = [p.name for p in presets]
            target_name, ok = QInputDialog.getItem(
                self, "Delete user preset",
                "Select preset to delete:", choices, 0, False,
            )
            if not ok or not target_name:
                return
            target = next((p for p in presets if p.name == target_name), None)
            if target is None:
                return
            if QMessageBox.question(
                self, "Delete preset",
                f"Delete '{target_name}' from {user_profile_dir()}?",
            ) == QMessageBox.StandardButton.Yes:
                if delete_user_profile(target):
                    self._emit_status(f"Deleted preset '{target_name}'", 4000)

    def _on_profile_load(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load profile",
            "",
            "JSON profiles (*.json);;All files (*)",
        )
        if not path:
            return
        try:
            profile = load_profile(path)
        except Exception as e:
            QMessageBox.critical(self, "Load profile failed", str(e))
            self._emit_status(f"Load profile failed: {e}", 6000)
            return
        self._apply_profile(profile)

    def _on_profile_save(self) -> None:
        name, ok = QInputDialog.getText(self, "Profile name", "Profile name:")
        if not ok or not name.strip():
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save profile",
            f"{name}.json",
            "JSON profiles (*.json);;All files (*)",
        )
        if not path:
            return

        profile = Profile(
            name=name.strip(),
            description="",
            params=self._local_panel.named_values(),
            pin_registers=self._local_panel.pin_values(),
            firmware=self._local_panel.family(),
        )
        try:
            save_profile(path, profile)
        except Exception as e:
            QMessageBox.critical(self, "Save profile failed", str(e))
            self._emit_status(f"Save profile failed: {e}", 6000)
            return
        self._emit_status(f"Saved profile '{name}' to {path}", 5000)

    # ---------------------------------------------------------------- radio signals
    def _on_params_loaded(self, result: object, is_remote: bool) -> None:
        s_params: dict[int, ParamValue] = getattr(result, "s_params", {}) or {}
        pin_params: dict[int, int] = getattr(result, "pin_params", {}) or {}
        # New in v3 of Ati5Result: the firmware's reported parameter names
        # so the panel can render correct labels per radio variant.
        s_names: dict[int, str] = getattr(result, "s_names", {}) or {}
        pin_names: dict[int, str] = getattr(result, "pin_names", {}) or {}
        family = str(getattr(result, "family", "") or FAMILY_SIK)
        meta = getattr(result, "s_meta", None)
        if not is_remote:
            self._set_family(family)

        # Only an explicit Load Settings overwrites unsaved edits; re-reads
        # done for Compare / Export / verify / connect keep them.
        explicit = (
            self._load_phase == ("remote" if is_remote else "local")
            or is_remote in self._overwrite_next
        )
        self._overwrite_next.discard(is_remote)
        panel = self._remote_panel if is_remote else self._local_panel
        if is_remote:
            self._remote_panel.setEnabled(True)
        panel.apply_values(
            s_params,
            pin_params,
            s_names=s_names if s_names else None,
            pin_names=pin_names if pin_names else None,
            family=family,
            meta=meta if meta else None,
            keep_dirty=not explicit,
        )
        if explicit:
            panel.mark_all_clean()

        # Cache the LOCAL snapshot so the validator's R19 (firmware lockdown
        # detection) can compare user-intended values against what the radio
        # currently has. We don't bother caching the remote values because
        # R19 is skipped for is_remote=True anyway.
        if not is_remote:
            self._radio_current_values = dict(s_params)

        side = "remote" if is_remote else "local"
        if is_remote and family == FAMILY_MULTICOMM and self._radio.remote_node is not None:
            side = f"remote NODEID {self._radio.remote_node}"
        self._emit_status(f"Loaded {side} settings ({len(s_params)} registers)", 5000)

        # Sequenced load: kick off the remote phase only after local succeeded
        # for THIS load operation.
        if self._load_phase == "local" and not is_remote:
            self._load_phase = "remote"
            self._emit_status("Loading remote settings…", 3000)
            self._radio.read_params(True)
        elif self._load_phase == "remote" and is_remote:
            self._load_phase = None

    def _on_params_failed(self, is_remote: bool, reason: str) -> None:
        self._overwrite_next.discard(is_remote)
        if self._load_phase == "remote" and is_remote:
            self._emit_status(f"Remote settings not loaded: {reason}", 6000)
            self._load_phase = None
        elif self._load_phase == "local" and not is_remote:
            self._load_phase = None

    def _on_write_result(self, sreg: int, value: object, ok: bool, is_remote: bool) -> None:
        # Match against either pin or s-register pending entry.
        keys = [
            (sreg, is_remote, False),
            (sreg, is_remote, True),
        ]
        is_pin = False
        matched = False
        for k in keys:
            if k in self._save_pending:
                self._save_pending.discard(k)
                is_pin = k[2]
                matched = True
                break

        if not matched:
            # Someone else's write (pairing wizard, Apply & Persist): not
            # ours to account for, and the panel row may hold other edits.
            return
        panel = self._remote_panel if is_remote else self._local_panel
        prefix = "R" if is_pin else "S"
        if ok:
            panel.mark_clean(sreg, is_pin)
        else:
            self._save_failures += 1
            self._save_rejected.append((f"{prefix}{sreg}", sreg, value))

        if matched and not self._save_pending:
            if self._save_failures == 0:
                self._emit_status(
                    "All changes saved — reboot the radio(s) to apply them."
                    if self._family == FAMILY_MULTICOMM else "All changes saved.",
                    6000,
                )
            else:
                self._announce_save_failures()
            self._save_failures = 0
            self._save_rejected.clear()

    def _announce_save_failures(self) -> None:
        """Build a single status-bar line naming the rejected registers.

        We deliberately don't pop a modal dialog here — the canonical place
        to learn about firmware-locked registers is the Validate button,
        which previews them *before* the user clicks Save.
        """
        labels = ", ".join(label for label, _sreg, _val in self._save_rejected)
        msg = (
            f"Save complete — {self._save_failures} register(s) rejected by the radio: {labels}. "
            "Click Validate for details."
        )
        self._emit_status(msg, 12000)

    def _on_eeprom_saved(self, ok: bool, is_remote: bool) -> None:
        side = "remote" if is_remote else "local"
        if not ok:
            self._emit_status(f"EEPROM save failed on {side}.", 6000)
        else:
            self._emit_status(f"EEPROM saved on {side}.", 5000)
        self._eeprom_pending.discard(is_remote)
        if not self._eeprom_pending and ok:
            self._emit_status("EEPROM save complete.", 5000)

    def _on_factory_reset_done(self, ok: bool, is_remote: bool) -> None:
        side = "remote" if is_remote else "local"
        if ok:
            self._emit_status(f"Factory reset on {side} succeeded.", 6000)
        else:
            self._emit_status(f"Factory reset on {side} failed.", 6000)

    def _on_radio_error(self, msg: str) -> None:
        self._emit_status(f"Radio error: {msg}", 7000)
        # An error during a load aborts the sequence so the user can retry.
        self._load_phase = None


__all__ = ["SettingsTab"]


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import sys

    app = QApplication.instance() or QApplication(sys.argv)
    radio = Radio()
    w = SettingsTab(radio)
    w.resize(1100, 700)
    w.show()
    sys.exit(app.exec())

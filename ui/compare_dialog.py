"""Side-by-side configuration comparison dialog.

Two main use cases:

* **Live ↔ JSON profile** — verify a saved/transferred config matches what
  the radio currently has.  Use this when the radios aren't yet linked,
  or when you've just imported a profile from another PC.
* **Live ↔ live remote** — when the radios *are* linked, run RTI5 to read
  the partner's config and compare.

Per-row "Stage value" buttons emit ``apply_fix_requested(sreg, value)``
(same pattern as ``ValidationDialog``) so the host settings tab can
stage the reference value on the local panel.

A "Stage all RF-critical fixes" button bundles every RF-rated mismatch
into a single signal so the host can run them through
``ApplyPersistController`` in one shot.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from rfd import multicomm
from rfd.diff import (
    SEVERITY_CONFLICT,
    SEVERITY_MISSING_A,
    SEVERITY_MISSING_B,
    SEVERITY_OK,
    SEVERITY_PER_NODE,
    SEVERITY_RF,
    SEVERITY_SOFT,
    SEVERITY_UART,
    CompareEntry,
    diff_configs,
    summarise,
)
from rfd.registers import FAMILY_MULTICOMM, get_spec


_BG_BY_SEVERITY = {
    SEVERITY_CONFLICT: "#f5b7b1",  # darker red
    SEVERITY_PER_NODE: "#f4f6f7",  # neutral
    SEVERITY_RF: "#ffd6d6",     # red
    SEVERITY_SOFT: "#ffe4b5",   # amber
    SEVERITY_UART: "#e0f0ff",   # blue
    SEVERITY_MISSING_A: "#f0f0f0",  # neutral
    SEVERITY_MISSING_B: "#f0f0f0",
    SEVERITY_OK: "#d4edda",     # green (only when match)
}

_ICON_BY_SEVERITY = {
    SEVERITY_CONFLICT: "⛔",
    SEVERITY_PER_NODE: "·",
    SEVERITY_RF: "🔴",
    SEVERITY_SOFT: "🟡",
    SEVERITY_UART: "🔵",
    SEVERITY_MISSING_A: "⚪",
    SEVERITY_MISSING_B: "⚪",
    SEVERITY_OK: "✓",
}

_LABEL_BY_SEVERITY = {
    SEVERITY_CONFLICT: "Node conflict (must differ)",
    SEVERITY_PER_NODE: "Per-node (may differ)",
    SEVERITY_RF: "RF-critical",
    SEVERITY_SOFT: "Link quality",
    SEVERITY_UART: "UART/GPIO",
    SEVERITY_MISSING_A: "Only on reference",
    SEVERITY_MISSING_B: "Only on this radio",
    SEVERITY_OK: "Match",
}


def _format_value(reg, value):
    """Render a value the same way the editor would (enum label + numeric)."""
    if value is None:
        return "—"
    if isinstance(value, tuple):
        text = ",".join(map(str, value))
        return text if len(text) <= 40 else text[:37] + "…"
    if reg is not None and reg.kind in ("enum", "bool") and reg.enum:
        label = reg.enum.get(value)
        if label is not None:
            return f"{label} ({value})"
    if reg is not None and reg.units:
        return f"{value} {reg.units}"
    return str(value)


class CompareConfigsDialog(QDialog):
    """Modal side-by-side diff with per-row and bulk fix actions."""

    apply_fix_requested = Signal(int, object)          # sreg, value
    apply_all_fixes_requested = Signal(object)         # list[(sreg, value)]

    def __init__(
        self,
        a_label: str,
        a_params: dict[str, int],
        a_sregs: dict[str, int],
        b_label: str,
        b_params: dict[str, int],
        b_sregs: dict[str, int],
        *,
        title: str = "Compare configurations",
        family: str | None = None,
        peer: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(820, 580)

        self._a_label = a_label
        self._b_label = b_label
        self._entries = diff_configs(
            a_params, b_params,
            a_sregs=a_sregs, b_sregs=b_sregs,
            family=family, peer=peer,
        )
        self._is_multicomm = family == FAMILY_MULTICOMM or multicomm.looks_like_multicomm(
            names=list(a_params) + list(b_params),
        )

        root = QVBoxLayout(self)

        header = self._build_header(self._entries)
        root.addWidget(header)

        # Column headers
        col_header = QFrame()
        col_layout = QHBoxLayout(col_header)
        col_layout.setContentsMargins(6, 4, 6, 4)
        for text, weight in (
            ("Parameter", 2),
            (a_label, 2),
            (b_label, 2),
            ("Action", 1),
        ):
            lbl = QLabel(f"<b>{text}</b>")
            lbl.setTextFormat(Qt.TextFormat.RichText)
            col_layout.addWidget(lbl, weight)
        root.addWidget(col_header)

        # Scrollable diff list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(6, 0, 6, 6)
        body_layout.setSpacing(2)
        for entry in self._entries:
            body_layout.addWidget(self._row_widget(entry))
        body_layout.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # Action bar
        bar = QHBoxLayout()
        rf_mismatches = [e for e in self._entries if e.severity == SEVERITY_RF]
        bulk_btn = QPushButton(
            f"Stage all {len(rf_mismatches)} RF-critical fix(es)"
        )
        bulk_btn.setEnabled(bool(rf_mismatches))
        bulk_btn.setToolTip(
            f"Set the local panel's RF parameters to match {b_label}. "
            "You'll still need to click Save Settings (or run Apply & Persist) "
            "to commit them."
        )
        bulk_btn.clicked.connect(self._on_bulk_fix)
        bar.addWidget(bulk_btn)
        bar.addStretch(1)

        buttons = QDialogButtonBox()
        close_btn = buttons.addButton("Close", QDialogButtonBox.ButtonRole.AcceptRole)
        close_btn.clicked.connect(self.accept)
        bar.addWidget(buttons)
        root.addLayout(bar)

    # ------------------------------------------------- header
    @staticmethod
    def _build_header(entries: list[CompareEntry]) -> QWidget:
        counts = summarise(entries)
        conflicts = counts.get(SEVERITY_CONFLICT, 0)
        rf = counts.get(SEVERITY_RF, 0)
        soft = counts.get(SEVERITY_SOFT, 0)
        uart = counts.get(SEVERITY_UART, 0)
        miss = counts.get(SEVERITY_MISSING_A, 0) + counts.get(SEVERITY_MISSING_B, 0)
        ok = counts.get(SEVERITY_OK, 0) + counts.get(SEVERITY_PER_NODE, 0)

        if conflicts == 0 and rf == 0 and soft == 0 and uart == 0 and miss == 0:
            text = (
                f"<span style='color:#27ae60;font-weight:bold;'>"
                f"✓ All {ok} parameter(s) match.</span>"
            )
        else:
            parts: list[str] = []
            if conflicts:
                parts.append(
                    f"<span style='color:#922b21;font-weight:bold;'>"
                    f"⛔ {conflicts} node conflict(s)</span>"
                )
            if rf:
                parts.append(
                    f"<span style='color:#c0392b;font-weight:bold;'>"
                    f"🔴 {rf} RF-critical</span>"
                )
            if soft:
                parts.append(
                    f"<span style='color:#d68910;'>🟡 {soft} link-quality</span>"
                )
            if uart:
                parts.append(
                    f"<span style='color:#2874a6;'>🔵 {uart} UART/GPIO</span>"
                )
            if miss:
                parts.append(f"<span style='color:#7f8c8d;'>⚪ {miss} present on one side only</span>")
            if ok:
                parts.append(
                    f"<span style='color:#27ae60;'>✓ {ok} match</span>"
                )
            text = " · ".join(parts)
        lbl = QLabel(text)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setWordWrap(True)
        return lbl

    # ------------------------------------------------- per-row
    def _row_widget(self, entry: CompareEntry) -> QWidget:
        row = QFrame()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(6, 3, 6, 3)
        spec = (
            multicomm.spec_for(-1, entry.name) if self._is_multicomm
            else get_spec(entry.name)
        )

        # Parameter label
        sreg = entry.sreg_a if entry.sreg_a is not None else entry.sreg_b
        sreg_str = f"S{sreg}" if sreg is not None else "—"
        param_lbl = QLabel(
            f"<code>{sreg_str}</code> {entry.name}<br>"
            f"<span style='color:#7f8c8d; font-size:10px;'>"
            f"{_LABEL_BY_SEVERITY[entry.severity]}</span>"
        )
        param_lbl.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(param_lbl, 2)

        # Side A value
        a_lbl = QLabel(_format_value(spec, entry.value_a))
        a_lbl.setStyleSheet(
            f"background-color: {_BG_BY_SEVERITY.get(entry.severity, 'transparent')};"
            "padding: 2px 6px;"
        )
        layout.addWidget(a_lbl, 2)

        # Side B value (icon prefix)
        b_text = f"{_ICON_BY_SEVERITY.get(entry.severity, '')} {_format_value(spec, entry.value_b)}"
        b_lbl = QLabel(b_text)
        b_lbl.setStyleSheet(
            f"background-color: {_BG_BY_SEVERITY.get(entry.severity, 'transparent')};"
            "padding: 2px 6px;"
        )
        layout.addWidget(b_lbl, 2)

        # Action — Stage button when there's a mismatch and we have a sreg
        # to write to on side A (the local).
        # No "Stage" for per-node values or conflicts: copying the other
        # node's NODEID/SLOTIDS would create (not fix) a collision.
        if entry.severity not in (
            SEVERITY_OK, SEVERITY_MISSING_B, SEVERITY_PER_NODE, SEVERITY_CONFLICT,
        ) and entry.sreg_a is not None and entry.value_b is not None:
            btn = QPushButton(f"Stage {_format_value(None, entry.value_b)}")
            btn.setToolTip(
                f"Set local S{entry.sreg_a} to match the reference "
                f"({entry.value_b})."
            )
            btn.clicked.connect(
                lambda *, s=entry.sreg_a, v=entry.value_b:
                    self.apply_fix_requested.emit(s, v)
            )
            layout.addWidget(btn, 1)
        else:
            spacer = QLabel("")
            layout.addWidget(spacer, 1)

        return row

    # ------------------------------------------------- bulk action
    def _on_bulk_fix(self) -> None:
        bundle: list[tuple[int, int]] = []
        for entry in self._entries:
            if entry.severity != SEVERITY_RF:
                continue
            if entry.sreg_a is None or entry.value_b is None:
                continue
            bundle.append((entry.sreg_a, entry.value_b))
        if bundle:
            self.apply_all_fixes_requested.emit(bundle)

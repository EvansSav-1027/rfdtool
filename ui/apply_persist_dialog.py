"""Modal dialog wrapping :class:`ApplyPersistController` with progress + log.

Drives a single apply-and-persist run from start to either DONE (with a
verification report) or FAILED.  Surfaces silent firmware-locked rejections
explicitly so the user understands why a write was acknowledged but didn't
take effect on a region-locked SKU.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from rfd.registers import REGISTERS
from .apply_persist import (
    ApplyPersistController,
    ApplyPersistReport,
    TOTAL_STAGES,
)


_OK_GREEN = "#27ae60"
_WARN_AMBER = "#d68910"
_ERR_RED = "#c0392b"
_NEUTRAL_GREY = "#7f8c8d"


def _fmt(value) -> str:
    if isinstance(value, tuple):
        text = ",".join(map(str, value))
        return text if len(text) <= 40 else text[:37] + "…"
    return str(value)


class ApplyPersistDialog(QDialog):
    """Run an :class:`ApplyPersistController` and display its progress.

    Emits ``apply_fix_requested(sreg, value)`` for each rejected register the
    user wants to "give up on" — the host settings tab catches this and
    stages the radio's actual value on the local panel so the panel matches
    reality.
    """

    # object: values may be tuples (MultiComm arrays) or exceed 32 bits.
    apply_fix_requested = Signal(int, object)

    def __init__(
        self,
        radio,
        intended: dict[int, int],
        *,
        firmware_banner: str = "",
        sreg_to_name: dict[int, str] | None = None,
        title: str = "Applying configuration",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(620, 480)

        self._intended = dict(intended)
        self._sreg_to_name = dict(sreg_to_name or {})
        self._controller = ApplyPersistController(radio, self)
        self._final_report: ApplyPersistReport | None = None

        root = QVBoxLayout(self)

        self._stage_label = QLabel("Preparing…")
        self._stage_label.setStyleSheet("font-weight: bold;")
        root.addWidget(self._stage_label)

        self._progress = QProgressBar()
        self._progress.setRange(0, TOTAL_STAGES)
        self._progress.setValue(0)
        self._progress.setFormat("%v / %m")
        root.addWidget(self._progress)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet("font-family: monospace; font-size: 11px;")
        root.addWidget(self._log, 1)

        self._summary = QLabel("")
        self._summary.setTextFormat(Qt.TextFormat.RichText)
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        # Container for per-register Apply-fix buttons (created on completion).
        self._fix_container = QWidget()
        fix_layout = QVBoxLayout(self._fix_container)
        fix_layout.setContentsMargins(0, 0, 0, 0)
        fix_layout.setSpacing(2)
        root.addWidget(self._fix_container)

        self._buttons = QDialogButtonBox()
        self._cancel_btn = self._buttons.addButton(
            "Cancel", QDialogButtonBox.ButtonRole.RejectRole
        )
        self._close_btn = self._buttons.addButton(
            "Close", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._close_btn.clicked.connect(self.accept)
        self._close_btn.setEnabled(False)
        root.addWidget(self._buttons)

        # Wire controller signals.
        self._controller.progress.connect(self._on_progress)
        self._controller.log.connect(self._on_log)
        self._controller.completed.connect(self._on_completed)
        self._controller.failed.connect(self._on_failed)

        # Kick off as soon as the dialog is shown.  Doing this in show()
        # rather than __init__ so the dialog has a chance to render before
        # any radio I/O blocks.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._start)
        self._firmware_banner = firmware_banner

    # -------------------------------------------------- public
    @property
    def report(self) -> ApplyPersistReport | None:
        return self._final_report

    def succeeded(self) -> bool:
        return self._final_report is not None and self._final_report.all_applied

    # -------------------------------------------------- start
    def _start(self) -> None:
        self._append_log(
            f"Applying {len(self._intended)} parameter(s) to the local radio…"
        )
        self._controller.start(self._intended, firmware_banner=self._firmware_banner)

    # -------------------------------------------------- controller signal handlers
    def _on_progress(self, label: str, step: int, total: int) -> None:
        self._progress.setMaximum(total)
        self._progress.setValue(step)
        self._stage_label.setText(label)

    def _on_log(self, msg: str) -> None:
        self._append_log(msg)

    def _on_completed(self, report: ApplyPersistReport) -> None:
        self._final_report = report
        self._progress.setValue(TOTAL_STAGES)
        self._cancel_btn.setEnabled(False)
        self._close_btn.setEnabled(True)
        self._render_summary(report)

    def _on_failed(self, reason: str) -> None:
        self._cancel_btn.setEnabled(False)
        self._close_btn.setEnabled(True)
        self._stage_label.setText("Failed")
        self._summary.setText(
            f"<span style='color:{_ERR_RED};'>"
            f"<b>✕ Apply failed:</b> {reason}</span>"
        )

    # -------------------------------------------------- summary builder
    def _render_summary(self, report: ApplyPersistReport) -> None:
        if report.all_applied:
            self._stage_label.setText("Complete")
            self._summary.setText(
                f"<span style='color:{_OK_GREEN};'>"
                f"<b>✓ All {len(report.accepted)} value(s) applied and verified "
                f"in {report.duration_s:.1f} s.</b></span>"
            )
            return

        accepted_n = len(report.accepted)
        rejected_n = len(report.rejected)
        locked_n = len(report.locked)

        parts: list[str] = []
        if accepted_n:
            parts.append(
                f"<span style='color:{_OK_GREEN};'>{accepted_n} accepted</span>"
            )
        if rejected_n:
            parts.append(
                f"<span style='color:{_WARN_AMBER};'>{rejected_n} not applied</span>"
            )
        if locked_n:
            parts.append(
                f"<span style='color:{_NEUTRAL_GREY};'>"
                f"{locked_n} firmware-locked</span>"
            )
        self._stage_label.setText("Complete with rejections")
        self._summary.setText(
            "Applied with mixed result: " + " · ".join(parts) +
            f" — completed in {report.duration_s:.1f} s."
        )

        # Build per-rejection rows with "Stage actual value" hooks so the
        # user can downgrade their panel to match what the radio kept.
        layout = self._fix_container.layout()
        if rejected_n:
            header = QLabel(
                f"<b>The radio kept different values for {rejected_n} "
                f"register(s):</b>"
            )
            header.setTextFormat(Qt.TextFormat.RichText)
            layout.addWidget(header)
            for sreg, intended, actual in report.rejected:
                row = QFrame()
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(8, 2, 8, 2)
                name = self._sreg_to_name.get(
                    sreg, REGISTERS[sreg].name if sreg in REGISTERS else f"S{sreg}"
                )
                lock_marker = " 🔒" if sreg in report.locked else ""
                held = "nothing (not reported)" if actual is None else _fmt(actual)
                lbl = QLabel(
                    f"<code>S{sreg:<2}</code> {name}{lock_marker}: "
                    f"intended <b>{_fmt(intended)}</b>, radio holds <b>{held}</b>"
                )
                lbl.setTextFormat(Qt.TextFormat.RichText)
                lbl.setWordWrap(True)
                row_layout.addWidget(lbl, 1)
                if actual is not None:
                    btn = QPushButton("Stage radio's value")
                    btn.setToolTip(
                        f"Set the panel's S{sreg} to the radio's actual value "
                        f"({_fmt(actual)}) so the panel matches reality."
                    )
                    btn.clicked.connect(
                        lambda *, s=sreg, v=actual:
                            self.apply_fix_requested.emit(s, v)
                    )
                    row_layout.addWidget(btn)
                layout.addWidget(row)

    # -------------------------------------------------- helpers
    def _append_log(self, msg: str) -> None:
        self._log.appendPlainText(msg)

    def _on_cancel(self) -> None:
        if self._controller.is_running():
            self._controller.cancel()
        else:
            self.reject()

    def reject(self) -> None:  # noqa: D401 — Qt override
        # Esc / window close mid-run: stop the pipeline rather than leaving
        # it writing and rebooting the radio behind a closed dialog.
        if self._controller.is_running():
            self._controller.cancel()
        super().reject()

"""Apply-and-persist state machine.

Wraps the existing async :class:`Radio` slots into one verified pipeline:

  WRITING (write_params_batch)
    ↓
  SAVING_EEPROM (save_eeprom)
    ↓
  REBOOTING (reboot — Radio internally polls AT until it comes back)
    ↓
  VERIFYING (read_params; compare to intended)
    ↓
  DONE  →  emits ApplyPersistReport with accepted / rejected / locked split

The "rejected" list is the value-add: even when the per-write ACK said OK,
firmware-locked SKUs (e.g. RFD900X2-US) silently ignore the write and the
post-reboot ATI5 reveals the truth.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from PySide6.QtCore import QObject, QTimer, Signal

from rfd import multicomm
from rfd.registers import FAMILY_MULTICOMM, coerce_value


class _State(Enum):
    IDLE = "idle"
    WRITING = "writing"
    SAVING_EEPROM = "saving_eeprom"
    REBOOTING = "rebooting"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"


# Stage indices for the progress signal (1-based to match user-facing 1/4 etc.)
STAGE_WRITE = 1
STAGE_SAVE = 2
STAGE_REBOOT = 3
STAGE_VERIFY = 4
TOTAL_STAGES = 4


# Per-stage timeouts (seconds).
TIMEOUT_WRITE = 30.0     # batch writes can take a while at low air rates
TIMEOUT_SAVE = 8.0
# Radio.reboot's own wait is 10 s of +++ attempts plus up to one attempt's
# overrun; the verify read needs a +++ bracket plus (MultiComm) ~10 ranged
# ATI5 reads.
TIMEOUT_REBOOT = 25.0
TIMEOUT_VERIFY = 15.0


@dataclass(frozen=True)
class ApplyPersistReport:
    intended: dict[int, int]
    final: dict[int, int]
    accepted: tuple[tuple[int, int], ...] = ()
    # (sreg, intended, actual); actual is None if the radio didn't report it.
    rejected: tuple[tuple[int, object, object], ...] = ()
    locked: tuple[int, ...] = ()                      # subset of rejected likely under firmware lockdown
    eeprom_saved_ok: bool = False
    rebooted: bool = False
    verified: bool = False
    duration_s: float = 0.0

    @property
    def all_applied(self) -> bool:
        return self.verified and not self.rejected


class ApplyPersistController(QObject):
    """Drive a Radio through write → save → reboot → verify."""

    progress = Signal(str, int, int)        # stage label, step, total
    log = Signal(str)
    completed = Signal(object)              # ApplyPersistReport
    failed = Signal(str)                    # human-readable reason

    def __init__(self, radio, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._radio = radio
        self._state = _State.IDLE
        self._intended: dict[int, int] = {}
        self._eeprom_saved_ok = False
        self._rebooted = False
        self._start_time = 0.0
        self._firmware_banner = ""

        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._on_timeout)

        # Wire radio signals once.  We filter by state inside each handler so
        # signals fired by unrelated activity (the rest of the UI) don't
        # confuse the state machine.
        self._radio.write_batch_done.connect(self._on_write_done)
        self._radio.eeprom_saved.connect(self._on_eeprom_saved)
        # Prefer the explicit reboot-finished signal: state_changed("data")
        # also fires when the AT&W bracket closes, which used to be taken
        # for "radio is back" while the reboot hadn't even started.
        self._reboot_signal = getattr(self._radio, "reboot_finished", None)
        if self._reboot_signal is not None:
            self._reboot_signal.connect(self._on_reboot_finished)
        else:
            self._radio.state_changed.connect(self._on_state_changed)
        self._radio.params_loaded.connect(self._on_params_loaded)
        self._radio.error.connect(self._on_radio_error)
        self._radio.log.connect(self._on_radio_log)

    # ------------------------------------------------------------ public
    @property
    def state(self) -> str:
        return self._state.value

    def is_running(self) -> bool:
        return self._state not in (_State.IDLE, _State.DONE, _State.FAILED)

    def start(
        self,
        intended: dict[int, int],
        *,
        firmware_banner: str = "",
    ) -> None:
        """Begin the apply-and-persist pipeline.

        ``intended`` is sreg-keyed (the caller has already translated names
        through the radio's actual ``name_to_sreg`` map).  ``firmware_banner``
        is used only to enrich the rejected-write classification — locked
        SKUs.
        """
        if self.is_running():
            self.failed.emit("apply-persist already running")
            return
        if not intended:
            self.failed.emit("no parameters to apply")
            return
        self._intended = dict(intended)
        self._eeprom_saved_ok = False
        self._rebooted = False
        self._start_time = time.monotonic()
        self._firmware_banner = firmware_banner

        self._enter(_State.WRITING)
        self.progress.emit(
            f"Writing {len(intended)} parameter(s)…",
            STAGE_WRITE, TOTAL_STAGES,
        )
        self.log.emit(
            f"→ writing {len(intended)} parameter(s) in one batch"
        )
        updates = [
            (int(s), coerce_value(v), False, False) for s, v in self._intended.items()
        ]
        self._radio.write_params_batch(updates)
        self._arm_watchdog(TIMEOUT_WRITE)

    def cancel(self) -> None:
        if not self.is_running():
            return
        self._fail("cancelled")

    # ------------------------------------------------------------ internals
    def _enter(self, state: _State) -> None:
        self._state = state

    def _arm_watchdog(self, seconds: float) -> None:
        self._watchdog.start(int(seconds * 1000))

    def _disarm_watchdog(self) -> None:
        self._watchdog.stop()

    def _on_timeout(self) -> None:
        self._fail(f"timeout in stage {self._state.value}")

    def _on_write_done(self, results) -> None:
        if self._state != _State.WRITING:
            return
        self._disarm_watchdog()
        rejected_at_write = [
            (s, v) for (s, v, _r, _p, ok) in results if not ok
        ]
        if rejected_at_write:
            self.log.emit(
                f"  {len(rejected_at_write)} write(s) rejected at command "
                f"time: {', '.join(f'S{s}' for s, _ in rejected_at_write)}"
            )
        else:
            self.log.emit("  all writes acknowledged")

        self._enter(_State.SAVING_EEPROM)
        if self._is_multicomm():
            # Radio.save_eeprom short-circuits: MultiComm persists on write.
            self.progress.emit("Settings persisted on write (MultiComm)…",
                               STAGE_SAVE, TOTAL_STAGES)
            self.log.emit("→ MultiComm saves on write — no AT&W needed")
        else:
            self.progress.emit("Saving to EEPROM (AT&W)…", STAGE_SAVE, TOTAL_STAGES)
            self.log.emit("→ AT&W")
        self._radio.save_eeprom(False)
        self._arm_watchdog(TIMEOUT_SAVE)

    def _on_eeprom_saved(self, ok: bool, is_remote: bool) -> None:
        if self._state != _State.SAVING_EEPROM or is_remote:
            return
        self._disarm_watchdog()
        if not ok:
            self._fail("EEPROM save (AT&W) returned error — settings will "
                       "revert on power cycle")
            return
        self._eeprom_saved_ok = True
        self.log.emit("  EEPROM saved")

        self._enter(_State.REBOOTING)
        self.progress.emit("Rebooting radio (ATZ)…", STAGE_REBOOT, TOTAL_STAGES)
        self.log.emit("→ ATZ; waiting for radio to come back…")
        self._radio.reboot(False)
        self._arm_watchdog(TIMEOUT_REBOOT)

    def _on_state_changed(self, state_name: str) -> None:
        # Radio.reboot transitions the radio back to STATE_DATA after its
        # internal _wait_for_radio() loop. That's our cue to verify.
        if self._state == _State.REBOOTING and state_name == "data":
            self._begin_verify(came_back=True)

    def _on_reboot_finished(self, came_back: bool, is_remote: bool) -> None:
        if self._state == _State.REBOOTING and not is_remote:
            self._begin_verify(came_back=came_back)

    def _begin_verify(self, *, came_back: bool) -> None:
        self._disarm_watchdog()
        self._rebooted = came_back
        self.log.emit(
            "  radio is back online" if came_back
            else "  radio didn't answer after reboot — trying to verify anyway"
        )

        self._enter(_State.VERIFYING)
        self.progress.emit("Verifying applied values…", STAGE_VERIFY, TOTAL_STAGES)
        self.log.emit("→ re-reading ATI5 to verify each parameter")
        self._radio.read_params(False)
        self._arm_watchdog(TIMEOUT_VERIFY)

    def _on_params_loaded(self, result, is_remote: bool) -> None:
        if self._state != _State.VERIFYING or is_remote:
            return
        self._disarm_watchdog()
        self._finish_verify(result)

    def _finish_verify(self, ati5) -> None:
        final = dict(getattr(ati5, "s_params", {}) or {})
        accepted: list[tuple[int, int]] = []
        rejected: list[tuple[int, object, object]] = []
        for sreg, intended_value in self._intended.items():
            actual = final.get(sreg)
            if actual == intended_value:
                accepted.append((sreg, intended_value))
            else:
                rejected.append((sreg, intended_value, actual))

        # Heuristic: any rejected sreg in the known firmware-lockdown set
        # for the connected SKU is reported separately so the dialog can
        # explain WHY (rather than just "didn't take").
        from rfd.regions import firmware_lockdown
        # The lockdown table describes SiK SKUs' sreg numbers; on MultiComm
        # those numbers mean different parameters.
        locked_set = (
            frozenset() if self._is_multicomm()
            else firmware_lockdown(self._firmware_banner)
        )
        if self._is_multicomm():
            for sreg, _i, actual in rejected:
                if sreg == multicomm.NAME_TO_SREG["TOTALUS"]:
                    self.log.emit(
                        f"  note: firmware adjusted TOTALUS to {actual} on reboot "
                        "(slot size limits) — make sure every node matches"
                    )
        locked = tuple(s for (s, _i, _a) in rejected if s in locked_set)

        report = ApplyPersistReport(
            intended=dict(self._intended),
            final=final,
            accepted=tuple(accepted),
            rejected=tuple(rejected),
            locked=locked,
            eeprom_saved_ok=self._eeprom_saved_ok,
            rebooted=self._rebooted,
            verified=True,
            duration_s=time.monotonic() - self._start_time,
        )
        self.log.emit(
            f"verify: {len(accepted)} accepted, {len(rejected)} rejected"
            + (f", {len(locked)} firmware-locked" if locked else "")
        )
        self._enter(_State.DONE)
        self.completed.emit(report)

    def _on_radio_error(self, msg: str) -> None:
        if self._state in (_State.IDLE, _State.DONE, _State.FAILED):
            return
        self._fail(f"radio error: {msg}")

    def _on_radio_log(self, msg: str, level: int) -> None:
        # Mirror radio's own log channel when relevant.
        if self._state in (_State.IDLE, _State.DONE, _State.FAILED):
            return
        # Indent so it's clear these are radio-side logs vs controller status.
        self.log.emit(f"  [radio] {msg}")

    def _is_multicomm(self) -> bool:
        return getattr(self._radio, "firmware_family", "") == FAMILY_MULTICOMM

    def _fail(self, reason: str) -> None:
        if self._state == _State.FAILED:
            return
        self._disarm_watchdog()
        self._enter(_State.FAILED)
        self.failed.emit(reason)

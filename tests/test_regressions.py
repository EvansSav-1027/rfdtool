"""Regression tests for bugs found in the verification pass.

Each test names the failure it guards against.  Qt-dependent tests run
offscreen.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from rfd import multicomm as mc
from rfd import protocol as proto
from rfd.diff import SEVERITY_RF, SEVERITY_SOFT, severity_for
from rfd.ihx import HexImage
from rfd.protocol import Ati5Result
from rfd.radio import Radio, RadioCore, RadioError, _has_ok
from rfd.registers import FAMILY_MULTICOMM
from rfd.uploader_8051 import UploadError, upload_8051
from rfd.validation import validate_config

FIXTURE = Path(__file__).parent / "fixtures" / "multicomm_v4_868_ati10.txt"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _spin(app, predicate, timeout_s=3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture(scope="module")
def mc_dump() -> Ati5Result:
    r = proto.parse_ati5(FIXTURE.read_text())
    return replace(r, s_names=mc.canonicalize_names(r.s_names), family=FAMILY_MULTICOMM)


# --------------------------------------------------------------------- radio core
def test_ok_detection_needs_an_ok_line():
    # Telemetry noise at the wrong baud used to pass for "in command mode".
    assert not _has_ok("x\x13ok\xffQ7Ok junk tOKen")
    assert _has_ok("+++\r\nOK\r\n")
    assert _has_ok("ATS3=4\r\nOK")


def test_idle_gap_waits_past_the_echo(mock_serial):
    # A remote's answer arrives a TDM round trip after the local echo; the
    # idle gap used to start on the echo and cut the reply off.
    core = RadioCore(mock_serial)

    def respond(_data: bytes) -> bytes:
        threading.Timer(0.4, lambda: mock_serial.feed("S1:BITRATE=64\r\nOK\r\n")).start()
        return b"RTI5:1:1\r\n"

    mock_serial.script(b"RTI5:1:1", respond)
    reply = core._send_collect(b"RTI5:1:1\r\n", timeout=2.0, idle_gap=0.2)
    assert "BITRATE=64" in reply


def test_sik_never_gets_node_suffix(mock_serial):
    # A MultiComm node selection must not leak into SiK RT commands.
    core = RadioCore(mock_serial)
    core.remote_node = 4
    mock_serial.script(b"RTS3=25", b"OK\r\n")
    assert core.write_param(3, 25, remote=True)
    assert b"RTS3=25\r\n" in mock_serial.written
    core.reboot(remote=True)
    assert b"RTZ\r\n" in mock_serial.written


def test_meta_fetch_gives_up_and_caches_negative(mock_serial):
    core = RadioCore(mock_serial)
    core.family = FAMILY_MULTICOMM
    mock_serial.script(b"ATI10:", b"OK\r\n")
    t0 = time.monotonic()
    assert core.read_param_meta(list(range(50))) == {}
    assert time.monotonic() - t0 < 4.0                 # not 50 × 0.8 s
    writes = mock_serial.written.count(b"ATI10:")
    assert core.read_param_meta(list(range(50))) == {}  # cached
    assert mock_serial.written.count(b"ATI10:") == writes


def test_remote_read_with_missing_chunk_raises(mock_serial):
    core = RadioCore(mock_serial)
    core.family = FAMILY_MULTICOMM
    core._remote_timing = lambda remote, t: (0.15, 0.05)
    plain = {
        int(l.split(":")[0][1:]): l
        for l in (
            f"S{m.group('sreg')}:{m.group('name')}={m.group('value')}"
            for m in map(proto.ATI5_LINE.search, FIXTURE.read_text().splitlines())
            if m
        )
    }

    def ranged(data: bytes) -> bytes:
        import re
        lo, hi = map(int, re.search(rb"TI5:(\d+):(\d+)", data).groups())
        if lo == 20:
            return b"RTI5:20:24\r\n"                    # lost over the air
        return ("".join(plain[s] + "\r\n" for s in range(lo, hi + 1) if s in plain)).encode()

    mock_serial.script(b"TI5:", ranged)
    with pytest.raises(RadioError, match="S20"):
        core.read_params(remote=True)


def test_hex_values_parse():
    assert proto.parse_ati5("S9:SLOTIDS=0x1F\r\n").s_params == {9: 31}


def test_rssi_report_switched_off_even_on_error(mock_serial):
    core = RadioCore(mock_serial)
    core.family = FAMILY_MULTICOMM

    def boom(*_a, **_k):
        raise OSError("unplugged")

    core._send_collect_for = boom
    with pytest.raises(OSError):
        core.poll_rssi()
    assert b"AT&T\r\n" in mock_serial.written


# --------------------------------------------------------------------- Radio (Qt)
def test_serial_failure_surfaces_as_error(qapp, mock_serial):
    # enter_command_mode raising used to vanish into an unread Future.
    radio = Radio(serial_factory=lambda p, b, t: mock_serial, mavlink_parsing=False)
    radio._core = RadioCore(mock_serial)
    radio._state = Radio.STATE_DATA
    mock_serial.close()
    errors: list[str] = []
    failed: list[tuple] = []
    radio.error.connect(errors.append)
    radio.params_failed.connect(lambda r, m: failed.append((r, m)))
    radio.read_params(False).result(timeout=5)
    assert _spin(qapp, lambda: errors and failed)
    assert "serial port error" in errors[0]
    radio.shutdown()


def test_typed_ato_returns_to_data_mode(qapp, mock_serial):
    radio = Radio(serial_factory=lambda p, b, t: mock_serial, mavlink_parsing=False)
    radio._core = RadioCore(mock_serial)
    radio._state = Radio.STATE_COMMAND
    mock_serial.script(b"ATO", b"OK\r\n")
    radio.send_raw_at("ATO").result(timeout=5)
    assert radio._state == Radio.STATE_DATA and not radio._hold_command
    radio.shutdown()


def test_terminal_hold_survives_background_ops(qapp, mock_serial):
    radio = Radio(serial_factory=lambda p, b, t: mock_serial, mavlink_parsing=False)
    radio._core = RadioCore(mock_serial)
    radio._state = Radio.STATE_COMMAND
    mock_serial.script(b"ATI\r\n", b"SiK\r\nOK\r\n")
    radio.send_raw_at("ATI").result(timeout=5)
    mock_serial.script(b"ATI7", b"L/R RSSI: 1/2\r\nOK\r\n")
    radio.poll_rssi().result(timeout=5)
    assert b"ATO" not in mock_serial.written      # didn't kick the terminal out
    radio.shutdown()


def test_failed_batch_still_reports_every_write(qapp, mock_serial):
    radio = Radio(serial_factory=lambda p, b, t: mock_serial, mavlink_parsing=False)
    radio._core = RadioCore(mock_serial)
    radio._state = Radio.STATE_DATA
    mock_serial.close()
    results: list[tuple] = []
    radio.write_result.connect(lambda *a: results.append(a))
    radio.write_params_batch([(3, 25, False, False), (4, 20, False, False)]).result(timeout=5)
    assert _spin(qapp, lambda: len(results) == 2)
    assert all(ok is False for _s, _v, ok, _r in results)
    radio.shutdown()


# --------------------------------------------------------------------- uploader
def test_banked_image_refused_before_erase(mock_serial):
    image = HexImage(chunks=[(0xFF00, b"\x00" * 0x200)], start_address=None,
                     min_address=0xFF00, max_address=0x100FF)
    with pytest.raises(UploadError, match="nothing was erased"):
        upload_8051(mock_serial, image)
    assert mock_serial.written == b""


# --------------------------------------------------------------------- validation
def _mc_titles(cfg: dict, names: dict, meta=None) -> dict[str, object]:
    report = validate_config(cfg, sreg_to_name=names, meta=meta)
    return {i.title: i for i in report.issues}


def test_out_of_range_values_are_reported(mc_dump):
    cfg = {**mc_dump.s_params, 30: 99, 10: 70}
    titles = _mc_titles(cfg, mc_dump.s_names, mc_dump.s_meta)
    assert "TXPOWER out of range" in titles and "NODEID out of range" in titles
    cfg = {**mc_dump.s_params, 1: 1000}          # radio allows 56..200
    assert "BITRATE out of range" in _mc_titles(cfg, mc_dump.s_names, mc_dump.s_meta)


def test_subnet_fix_targets_subnets(mc_dump):
    cfg = {**mc_dump.s_params, 11: 2, 12: 1}
    issue = _mc_titles(cfg, mc_dump.s_names)["SUBNETID ≥ SUBNETS"]
    assert issue.sregs[0] == 12 and issue.suggested_value == 3


def test_listen_only_master_flagged(mc_dump):
    cfg = {**mc_dump.s_params, 6: 0, 10: 0, 19: 0}
    assert "Listen-only node is the master" in _mc_titles(cfg, mc_dump.s_names)
    cfg = {**mc_dump.s_params, 6: 0, 10: 0, 19: 3}
    assert "Master can't transmit sync" in _mc_titles(cfg, mc_dump.s_names)


def test_slot_fix_only_when_achievable(mc_dump):
    cfg = {**mc_dump.s_params, 1: 2000, 7: 5000}
    issue = _mc_titles(cfg, mc_dump.s_names)["Slot size out of range"]
    assert issue.suggested_value is None


def test_lower_duty_cycle_is_fine():
    report = validate_config({8: 902000, 9: 928000, 10: 50, 11: 50})
    assert not any("DUTY_CYCLE" in i.title for i in report.issues)


def test_868_bands_recognised():
    assert validate_config({8: 865000, 9: 868000}).detected_region.code == "IN865"


def test_sik_must_match_list():
    assert severity_for("LBT_RSSI") == SEVERITY_RF
    assert severity_for("ENCRYPTION_LEVEL") == SEVERITY_RF
    assert severity_for("MAVLINK") == SEVERITY_SOFT


# --------------------------------------------------------------------- apply & persist
class _RebootStub(QObject):
    write_batch_done = Signal(object)
    eeprom_saved = Signal(bool, bool)
    state_changed = Signal(str)
    params_loaded = Signal(object, bool)
    reboot_finished = Signal(bool, bool)
    error = Signal(str)
    log = Signal(str, int)
    firmware_family = "sik"

    def __init__(self):
        super().__init__()
        self.reads = 0
        self.finish_reboot = None

    def write_params_batch(self, updates):
        QTimer.singleShot(0, lambda: self.write_batch_done.emit(
            [(s, v, r, p, True) for s, v, r, p in updates]))

    def save_eeprom(self, is_remote):
        def done():
            self.eeprom_saved.emit(True, is_remote)
            # The real AT&W bracket closes *after* the save reply.
            self.state_changed.emit("data")
        QTimer.singleShot(0, done)

    def reboot(self, is_remote):
        self.finish_reboot = lambda: self.reboot_finished.emit(True, is_remote)

    def read_params(self, is_remote):
        self.reads += 1
        QTimer.singleShot(0, lambda: self.params_loaded.emit(
            Ati5Result({3: 41}, {}, {3: "NETID"}, {}), is_remote))


def test_stale_data_state_is_not_reboot_complete(qapp):
    from ui.apply_persist import ApplyPersistController
    radio = _RebootStub()
    ctl = ApplyPersistController(radio)
    done: list = []
    ctl.completed.connect(done.append)
    ctl.start({3: 41})
    assert _spin(qapp, lambda: radio.finish_reboot is not None)
    _spin(qapp, lambda: False, timeout_s=0.2)
    assert radio.reads == 0, "verified before the reboot finished"
    radio.finish_reboot()
    assert _spin(qapp, lambda: done)
    assert done[0].all_applied


# --------------------------------------------------------------------- settings tab
@pytest.fixture
def tab(qapp):
    from ui.settings_tab import SettingsTab
    radio = Radio(mavlink_parsing=False)
    t = SettingsTab(radio)
    yield t
    radio.shutdown()


def _sik_result() -> Ati5Result:
    names = {0: "FORMAT", 1: "SERIAL_SPEED", 2: "AIR_SPEED", 3: "NETID", 4: "TXPOWER"}
    return Ati5Result({0: 25, 1: 57, 2: 64, 3: 25, 4: 20}, {}, names, {})


def test_dirty_edits_follow_names_not_sregs(tab, mc_dump):
    tab._local_panel.set_value(1, 115)               # SiK S1 SERIAL_SPEED edit
    tab._on_params_loaded(mc_dump, False)            # now a MultiComm radio
    assert tab._local_panel.get_value(1) == 200      # BITRATE untouched
    assert not tab._local_panel.dirty()


def test_sik_after_multicomm_restores_pins_and_node(tab, mc_dump):
    tab._on_radio_info({"firmware_family": "multicomm"})
    tab._on_params_loaded(mc_dump, False)
    tab._node_spin.setValue(4)
    assert tab._radio.remote_node == 4
    tab._on_radio_info({"firmware_family": "sik"})
    tab._on_params_loaded(_sik_result(), False)
    assert tab._radio.remote_node is None
    assert any(is_pin for (_s, is_pin) in tab._local_panel.rows())


def test_node_change_drops_stale_remote_edits(tab, mc_dump):
    tab._on_radio_info({"firmware_family": "multicomm"})
    tab._on_params_loaded(mc_dump, True)
    tab._node_spin.setValue(2)
    tab._remote_panel.set_value(10, 2)
    tab._node_spin.setValue(5)
    assert not tab._remote_panel.dirty()
    assert not tab._remote_panel.isEnabled()
    tab._on_params_loaded(mc_dump, True)
    assert tab._remote_panel.isEnabled()


def test_background_reread_keeps_unsaved_edits(tab, mc_dump):
    tab._on_params_loaded(mc_dump, False)
    tab._local_panel.set_value(30, 20)
    tab._on_params_loaded(mc_dump, False)            # e.g. Compare's re-read
    assert tab._local_panel.get_value(30) == 20 and (30, False) in tab._local_panel.dirty()
    tab._load_phase = "local"                         # explicit Load Settings
    tab._on_params_loaded(mc_dump, False)
    assert tab._local_panel.get_value(30) == 30 and not tab._local_panel.dirty()


def test_foreign_write_results_leave_panel_alone(tab, mc_dump):
    tab._on_params_loaded(mc_dump, False)
    tab._local_panel.set_value(30, 20)
    tab._on_write_result(30, 28, True, False)        # e.g. the wizard's batch
    assert (30, False) in tab._local_panel.dirty()


def test_wide_int_range_does_not_crash(tab):
    from rfd.registers import ParamMeta
    names = {0: "FORMAT", 1: "BITRATE", 60: "FUTURE"}
    res = Ati5Result({0: 7, 1: 64, 60: 3_000_000_000}, {}, names, {},
                     family=FAMILY_MULTICOMM,
                     s_meta={60: ParamMeta("N", 0, 4_294_967_295)})
    tab._on_params_loaded(res, False)
    assert tab._local_panel.get_value(60) == 3_000_000_000


# --------------------------------------------------------------------- wizard
def test_wizard_ignores_background_stage_completion(qapp):
    from ui.pairing_wizard import PairingWizard
    radio = Radio(mavlink_parsing=False)
    wiz = PairingWizard(
        radio, name_to_sreg_provider=dict, sreg_to_name_provider=dict,
        firmware_banner_provider=str, board_name_provider=str,
    )
    from rfd.protocol import RssiReport
    for _ in range(4):
        radio.rssi_received.emit(RssiReport(linked=True))
        _spin(qapp, lambda: False, timeout_s=0.05)
    assert wiz._stack.currentIndex() == 0
    wiz.reject()
    radio.shutdown()


# --------------------------------------------------------------------- RSSI polling session
_FAST = proto.CommandModeBracket(quiet_before=0.01, quiet_after=0.01, reply_timeout=0.3)


@pytest.fixture
def session_radio(qapp, mock_serial, monkeypatch):
    monkeypatch.setattr(proto, "CommandModeBracket", lambda: _FAST)
    mock_serial.script(b"+++", b"OK\r\n")
    mock_serial.script(b"ATO", b"OK\r\n")
    radio = Radio(serial_factory=lambda p, b, t: mock_serial, mavlink_parsing=False)
    radio._core = RadioCore(mock_serial)
    radio._state = Radio.STATE_DATA
    yield radio
    radio.shutdown()


def test_sik_session_enters_command_mode_once(qapp, mock_serial, session_radio):
    # Interval < 2 s used to be impossible: every poll paid the +++ bracket.
    radio = session_radio
    mock_serial.script(b"ATI7", b"L/R RSSI: 200/190  L/R noise: 30/31 pkts: 9\r\nOK\r\n")
    radio.start_rssi_session().result(timeout=5)
    reports: list = []
    radio.rssi_received.connect(reports.append)
    t0 = time.monotonic()
    for _ in range(5):
        radio.poll_rssi().result(timeout=5)
    assert (time.monotonic() - t0) / 5 < 0.5          # well under the old ~2.5 s
    assert _spin(qapp, lambda: len(reports) == 5)
    assert reports[-1].local_rssi == 200
    assert mock_serial.written.count(b"+++") == 1
    assert b"ATO" not in mock_serial.written
    radio.stop_rssi_session().result(timeout=5)
    assert mock_serial.written.count(b"ATO") == 1
    assert radio._state == Radio.STATE_DATA


def test_multicomm_session_reads_streamed_report(qapp, mock_serial, session_radio):
    radio = session_radio
    radio._core.family = FAMILY_MULTICOMM
    radio.start_rssi_session().result(timeout=5)
    assert mock_serial.written.count(b"AT&T=RSSI") == 1
    reports: list = []
    radio.rssi_received.connect(reports.append)
    mock_serial.feed("Loc RSSI Noise Av:95.00 raw:-96\r\nLoc RSSI Node:1 Av:61.00 raw:-61\r\n")
    before = len(mock_serial.written)
    radio.poll_rssi().result(timeout=5)
    assert len(mock_serial.written) == before          # nothing sent per poll
    assert _spin(qapp, lambda: reports)
    assert reports[0].local_rssi == -61 and reports[0].linked is True
    radio.stop_rssi_session().result(timeout=5)
    assert b"AT&T\r\n" in mock_serial.written


def test_partial_report_line_is_kept_for_next_poll(mock_serial):
    core = RadioCore(mock_serial)
    core.family = FAMILY_MULTICOMM
    mock_serial.feed("Loc RSSI Node:1 Av:6")
    assert core.read_rssi_report().linked is None
    mock_serial.feed("2.00 raw:-62\r\n")
    assert core.read_rssi_report().local_rssi == -62


def test_terminal_and_session_holds_are_independent(qapp, mock_serial, session_radio):
    radio = session_radio
    mock_serial.script(b"ATI\r\n", b"SiK\r\nOK\r\n")
    radio.start_rssi_session().result(timeout=5)
    radio.send_raw_at("ATI").result(timeout=5)
    radio.back_to_data().result(timeout=5)             # terminal lets go…
    assert b"ATO" not in mock_serial.written           # …session still holds
    radio.stop_rssi_session().result(timeout=5)
    assert mock_serial.written.count(b"ATO") == 1


def test_rssi_tab_opens_session_only_when_visible_and_unpaused(qapp):
    from ui.rssi_tab import RssiTab

    class _R(QObject):
        mavlink_radio_status = Signal(object)
        rssi_received = Signal(object)
        state_changed = Signal(str)
        radio_info = Signal(object)

        def __init__(self):
            super().__init__()
            self.calls: list[str] = []

        def start_rssi_session(self):
            self.calls.append("start")

        def stop_rssi_session(self):
            self.calls.append("stop")

        def poll_rssi(self):
            return None

    radio = _R()
    tab = RssiTab(radio)
    radio.state_changed.emit(Radio.STATE_DATA)
    tab._ati7_radio.setChecked(True)
    assert radio.calls == []                           # not visible yet
    tab.show()
    qapp.processEvents()
    assert radio.calls == ["start"]
    tab._pause_btn.setChecked(True)
    assert radio.calls == ["start", "stop"]
    tab._pause_btn.setChecked(False)
    tab._mav_radio.setChecked(True)
    assert radio.calls == ["start", "stop", "start", "stop"]
    tab.close()


def test_disconnect_leaves_command_mode(qapp, mock_serial, session_radio):
    radio = session_radio
    radio._core.family = FAMILY_MULTICOMM
    radio.start_rssi_session().result(timeout=5)
    radio.close_port().result(timeout=5)
    assert b"AT&T\r\n" in mock_serial.written           # report off
    assert mock_serial.written.endswith(b"ATO\r\n")     # then back to data

"""Tests for the .gbl command-line flasher against a simulated x-series radio."""
from __future__ import annotations

import binascii
import csv
import sys

import pytest

from rfd import gbl_flash as gf

BANNER = "RFD900xSub:2:MainV1.12.03 ChipID: 980C33FFFE178A11"
FAST = gf.Timing(banner_read=0.2, guard=0.0, ok_wait=0.2, bootloader_wait=0.6,
                 ready_wait=0.3, ack_timeout=0.3, after_cancel=0.0,
                 after_bootnew=0.0, ati_wait=0.2)


class FakeRadio:
    """pyserial stand-in speaking the RFD bootloader + XMODEM-CRC receiver."""

    def __init__(self, *, mode: str = "boot", nak_first: int = 0,
                 text_first: bool = False, cancel_attempts: int = 0,
                 silent_firmware: bool = False) -> None:
        self.mode = mode                    # "boot" | "fw" | "dead"
        self.baudrate = 57600
        self.timeout = 1.0
        self.rx = bytearray()               # bytes the host will read
        self.line = bytearray()
        self.xmodem = False
        self.received = bytearray()
        self.expect_seq = 1
        self.nak_first = nak_first
        self.text_first = text_first
        self.cancel_attempts = cancel_attempts
        self.silent_firmware = silent_firmware
        self.uploads = 0
        self.c_sent = 0
        self.written = bytearray()
        self.closed = False

    # ---- pyserial API
    @property
    def in_waiting(self) -> int:
        return len(self.rx)

    def read(self, size: int = 1) -> bytes:
        out = bytes(self.rx[:size])
        del self.rx[:size]
        return out

    def reset_input_buffer(self) -> None:
        self.rx.clear()

    def flush(self) -> None: ...
    def close(self) -> None:
        self.closed = True

    def write(self, data: bytes) -> int:
        self.written += data
        if self.mode == "dead":
            return len(data)
        if self.xmodem:
            self._xmodem(bytes(data))
            return len(data)
        if self.mode == "fw" and data == b"+++":
            if not self.silent_firmware:
                self.rx += b"OK\r\n"
            return len(data)
        self.line += data
        while b"\r\n" in self.line:
            cmd, _, rest = bytes(self.line).partition(b"\r\n")
            self.line = bytearray(rest)
            self._command(cmd.decode())
        return len(data)

    # ---- behaviour
    def _command(self, cmd: str) -> None:
        if self.mode == "boot":
            if cmd == "":
                self.rx += (BANNER + "\r\n").encode()
            elif cmd == "UPLOAD":
                self.uploads += 1
                self.xmodem = True
                self.expect_seq = 1
                self.received.clear()
                self.rx += b"Ready\r\nC"     # one 'C' only: the host mustn't eat it
                self.c_sent += 1
            elif cmd == "BOOTNEW":
                self.mode = "fw"
        elif self.mode == "fw" and not self.silent_firmware:
            if cmd == "ATI":
                self.rx += b"ATI\r\nRFD MultiComm v4.00 on RFD868x\r\nOK\r\n"
            elif cmd == "ATO":
                self.rx += b"OK\r\n"
            elif cmd == "AT&UPDATE":
                self.mode = "boot"

    def _xmodem(self, data: bytes) -> None:
        if data == gf.EOT:
            self.rx += gf.ACK
            self.xmodem = False
            return
        if data.startswith(gf.CAN):
            self.xmodem = False
            return
        assert data[0] == 0x01 and len(data) == 133, data[:4]
        seq, inv, block, crc = data[1], data[2], data[3:131], data[131:]
        assert seq == (~inv & 0xFF)
        assert int.from_bytes(crc, "big") == binascii.crc_hqx(block, 0)
        if self.cancel_attempts and self.uploads <= self.cancel_attempts:
            self.rx += gf.CAN + gf.CAN
            return
        if self.text_first:
            self.text_first = False
            self.rx += b"NAK on sector\r\n"
            return
        if self.nak_first:
            self.nak_first -= 1
            self.rx += gf.NAK
            return
        if seq == self.expect_seq & 0xFF:
            self.received += block
            self.expect_seq += 1
        self.rx += gf.ACK


def _image(n: int = 1000) -> bytes:
    return bytes((i * 7) & 0xFF for i in range(n))


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(gf, "Timing", lambda: FAST)


@pytest.fixture
def gbl(tmp_path):
    p = tmp_path / "fw.gbl"
    p.write_bytes(_image())
    return p


def _run(monkeypatch, radio, argv, inputs=()):
    monkeypatch.setattr(gf, "open_port", lambda port, baud: radio)
    answers = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda *_a: next(answers))
    return gf.main(argv)


# --------------------------------------------------------------------- unit
def test_parse_banner():
    b = gf.parse_banner("junk\r\n" + BANNER + "\r\n")
    assert b.model == "RFD900xSub:2:MainV1.12.03" and b.chip_id == "980C33FFFE178A11"
    assert gf.parse_banner("ATI\r\nOK") is None


def test_happy_path_transfers_exact_image():
    radio = FakeRadio()
    fl = gf.GblFlasher(radio, timing=FAST, log=lambda *_: None)
    assert fl.find_bootloader().chip_id == "980C33FFFE178A11"
    ok, msgs = fl.upload(_image())
    assert ok, msgs
    assert radio.c_sent == 1                       # the single 'C' reached the lib
    assert bytes(radio.received[:1000]) == _image()
    assert set(radio.received[1000:]) == {0x1A}    # standard XMODEM padding
    fl.boot_new()
    assert fl.verify() == "RFD MultiComm v4.00 on RFD868x"
    assert radio.written.endswith(b"ATO\r\n")


def test_naks_are_retried_within_one_attempt():
    radio = FakeRadio(nak_first=3)
    fl = gf.GblFlasher(radio, timing=FAST, log=lambda *_: None)
    ok, _ = fl.upload(_image())
    assert ok and bytes(radio.received[:1000]) == _image()


def test_bootloader_text_reply_counts_as_one_nak():
    # "NAK on sector" fed byte-by-byte would desync the transfer.
    radio = FakeRadio(text_first=True)
    logs: list[str] = []
    fl = gf.GblFlasher(radio, timing=FAST, log=logs.append)
    ok, msgs = fl.upload(_image())
    assert ok and msgs == ["NAK on sector"]
    assert bytes(radio.received[:1000]) == _image()


def test_radio_cancel_aborts_attempt():
    radio = FakeRadio(cancel_attempts=1)
    fl = gf.GblFlasher(radio, timing=FAST, log=lambda *_: None)
    ok, msgs = fl.upload(_image())
    assert not ok and "CAN" in msgs[-1]


def test_enters_bootloader_from_firmware():
    radio = FakeRadio(mode="fw")
    fl = gf.GblFlasher(radio, timing=FAST, log=lambda *_: None)
    assert fl.find_bootloader().model.startswith("RFD900x")
    assert b"+++" in radio.written and b"AT&UPDATE\r\n" in radio.written


def test_banner_waits_for_full_chip_id():
    radio = FakeRadio()
    fl = gf.GblFlasher(radio, timing=FAST, log=lambda *_: None)
    # Banner arriving in two pieces must not be cut at the first piece.
    orig = radio._command

    def split(cmd):
        if cmd == "":
            radio.rx += BANNER[:-6].encode()
            import threading
            threading.Timer(0.05, lambda: radio.rx.extend(BANNER[-6:].encode() + b"\r\n")).start()
        else:
            orig(cmd)

    radio._command = split
    assert fl.read_banner().chip_id == "980C33FFFE178A11"


# --------------------------------------------------------------------- CLI flow
def test_cli_success_exit_0(monkeypatch, gbl, capsys):
    radio = FakeRadio()
    assert _run(monkeypatch, radio, ["--port", "/dev/x", "--file", str(gbl), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "1,000 bytes, 8 blocks" in out and "new firmware: RFD MultiComm" in out
    assert radio.closed


def test_cli_retries_after_failed_attempt(monkeypatch, gbl):
    radio = FakeRadio(cancel_attempts=1)
    assert _run(monkeypatch, radio, ["--port", "/dev/x", "--file", str(gbl), "--yes"]) == 0
    assert radio.uploads == 2
    assert gf.CAN * 3 in radio.written            # clean abort between attempts


def test_cli_gives_up_after_three(monkeypatch, gbl):
    radio = FakeRadio(cancel_attempts=99)
    code = _run(monkeypatch, radio, ["--port", "/dev/x", "--file", str(gbl), "--yes"])
    assert code == gf.EXIT_UPLOAD and radio.uploads == 3


def test_cli_confirmation_required(monkeypatch, gbl):
    radio = FakeRadio()
    code = _run(monkeypatch, radio, ["--port", "/dev/x", "--file", str(gbl)], ["n"])
    assert code == gf.EXIT_DECLINED and radio.uploads == 0


def test_cli_no_radio(monkeypatch, gbl, capsys):
    code = _run(monkeypatch, FakeRadio(mode="dead"),
                ["--port", "/dev/x", "--file", str(gbl), "--yes"])
    assert code == gf.EXIT_NO_BOOTLOADER
    assert "pad 9" in capsys.readouterr().err


def test_cli_verify_failure(monkeypatch, gbl):
    radio = FakeRadio(silent_firmware=True)
    code = _run(monkeypatch, radio, ["--port", "/dev/x", "--file", str(gbl), "--yes"])
    assert code == gf.EXIT_VERIFY


def test_cli_rejects_non_gbl(monkeypatch, tmp_path):
    f = tmp_path / "fw.bin"
    f.write_bytes(b"x")
    assert gf.main(["--port", "/dev/x", "--file", str(f)]) == gf.EXIT_USAGE


def test_dry_run_on_firmware_doesnt_enter_bootloader(monkeypatch, capsys):
    radio = FakeRadio(mode="fw")
    assert _run(monkeypatch, radio, ["--port", "/dev/x", "--dry-run"]) == 0
    assert b"AT&UPDATE" not in radio.written and radio.mode == "fw"
    assert "running firmware" in capsys.readouterr().out


def test_batch_logs_each_radio(monkeypatch, gbl, tmp_path):
    radios = iter([FakeRadio(), FakeRadio(cancel_attempts=99)])
    monkeypatch.setattr(gf, "open_port", lambda port, baud: next(radios))
    answers = iter(["", "q"])
    monkeypatch.setattr("builtins.input", lambda *_a: next(answers))
    log = tmp_path / "log.csv"
    code = gf.main(["--port", "/dev/x", "--file", str(gbl), "--yes", "--batch",
                    "--log", str(log)])
    rows = list(csv.DictReader(log.open()))
    assert [r["result"] for r in rows] == ["ok", f"fail({gf.EXIT_UPLOAD})"]
    assert rows[0]["chip_id"] == "980C33FFFE178A11"
    assert rows[0]["firmware"] == "RFD MultiComm v4.00 on RFD868x"
    assert code == gf.EXIT_UPLOAD


def test_entry_point_dispatch_skips_qt(monkeypatch):
    import rfdtool
    monkeypatch.delitem(sys.modules, "PySide6.QtWidgets", raising=False)
    assert rfdtool.main(["--port", "/dev/does-not-exist", "--dry-run"]) == gf.EXIT_PORT

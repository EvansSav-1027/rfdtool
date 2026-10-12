"""Tests for the SiK 8051 bootloader uploader.

The mock serial is scripted so that each command's reply lands in the read
buffer as soon as the command is written. We never test against a live port.
"""
from __future__ import annotations

from typing import Callable

import pytest

from rfd import ihx
from rfd.uploader_8051 import (
    CHIP_ERASE,
    EOC,
    FAILED,
    GET_DEVICE,
    GET_SYNC,
    INSYNC,
    LOAD_ADDRESS,
    OK,
    PROG_MULTI,
    PROG_MULTI_MAX,
    REBOOT,
    UploadCancelled,
    UploadError,
    chip_erase,
    get_device,
    get_sync,
    load_address,
    prog_multi,
    reboot,
    upload_8051,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auto_responder(get_device_payload: bytes = bytes([0x4E, 0x43])) -> Callable[[bytes], bytes]:
    """Return a callable that, given a single write payload, returns a reply.

    The MockSerial's ``script`` machinery hands every ``write()`` payload to
    this function; we recognise the leading opcode and synthesise a reply.
    Commands without replies (REBOOT) return ``b""``.
    """
    def respond(data: bytes) -> bytes:
        if not data:
            return b""
        op = data[0]
        if op == GET_SYNC:
            return bytes([INSYNC, OK])
        if op == GET_DEVICE:
            return get_device_payload + bytes([INSYNC, OK])
        if op == CHIP_ERASE:
            return bytes([INSYNC, OK])
        if op == LOAD_ADDRESS:
            return bytes([INSYNC, OK])
        if op == PROG_MULTI:
            return bytes([INSYNC, OK])
        if op == REBOOT:
            return b""
        return b""
    return respond


def _install_auto(mock_serial, **kwargs) -> None:
    """Hook the auto responder onto every write()."""
    # Trigger byte b"" never matches via ``in``, but we want EVERY write
    # processed. Use a single-byte trigger that *will* be present: every
    # command we care about ends with EOC=0x20.
    mock_serial.script(bytes([EOC]), _auto_responder(**kwargs))


def _build_image(chunks: list[tuple[int, bytes]]):
    """Build a HexImage by emitting Intel HEX records and parsing them.

    Using the real parser keeps us honest about chunk shapes and ordering.
    """
    lines: list[str] = []
    for addr, data in chunks:
        # one record per chunk - keep it simple, byte_count fits in 1 byte
        assert len(data) <= 0xFF
        rec = bytearray([len(data), (addr >> 8) & 0xFF, addr & 0xFF, 0x00])
        rec.extend(data)
        cs = (-(sum(rec) & 0xFF)) & 0xFF
        rec.append(cs)
        lines.append(":" + rec.hex().upper())
    lines.append(":00000001FF")  # EOF
    return ihx.parse("\n".join(lines))


# ---------------------------------------------------------------------------
# get_sync
# ---------------------------------------------------------------------------

def test_get_sync_happy_path(mock_serial):
    # get_sync drains the input buffer before probing, so feed via script
    # rather than pre-buffering.
    mock_serial.script(bytes([GET_SYNC, EOC]), bytes([INSYNC, OK]))
    get_sync(mock_serial)
    assert bytes(mock_serial.written) == bytes([GET_SYNC, EOC])


def test_get_sync_retries_then_succeeds(mock_serial):
    # Two attempts of garbage, then a clean reply on the third write.
    state = {"calls": 0}

    def respond(data: bytes) -> bytes:
        state["calls"] += 1
        if state["calls"] == 1:
            return bytes([0xAA, 0xBB])  # bogus, neither INSYNC
        if state["calls"] == 2:
            return bytes([INSYNC, 0xFF])  # INSYNC but wrong follow-up
        return bytes([INSYNC, OK])

    mock_serial.script(bytes([GET_SYNC, EOC]), respond)
    get_sync(mock_serial, retries=5)
    assert state["calls"] == 3
    # Each attempt writes GET_SYNC + EOC.
    assert mock_serial.written.count(bytes([GET_SYNC, EOC])) == 3


def test_get_sync_exhausted_raises(mock_serial):
    # No data fed - every read times out.
    with pytest.raises(UploadError):
        get_sync(mock_serial, retries=2, timeout=0.01)
    # Two attempts written.
    assert mock_serial.written.count(bytes([GET_SYNC, EOC])) == 2


# ---------------------------------------------------------------------------
# get_device
# ---------------------------------------------------------------------------

def test_get_device_parses_payload(mock_serial):
    # Real SiK bootloader order: board_id, freq_id, INSYNC, OK.
    mock_serial.feed(bytes([0x4E, 0x43, INSYNC, OK]))
    board_id, freq_id = get_device(mock_serial)
    assert board_id == 0x4E
    assert freq_id == 0x43
    assert bytes(mock_serial.written) == bytes([GET_DEVICE, EOC])


def test_get_device_failed_raises(mock_serial):
    mock_serial.feed(bytes([0x00, 0x00, INSYNC, FAILED]))
    with pytest.raises(UploadError):
        get_device(mock_serial)


# ---------------------------------------------------------------------------
# load_address
# ---------------------------------------------------------------------------

def test_load_address_little_endian(mock_serial):
    mock_serial.feed(bytes([INSYNC, OK]))
    load_address(mock_serial, 0x1234)
    assert bytes(mock_serial.written) == bytes([LOAD_ADDRESS, 0x34, 0x12, EOC])


def test_load_address_rejects_oversized():
    class Dummy:
        timeout = 1.0
        def read(self, n=1): return b""
        def write(self, data): return len(data)
        def reset_input_buffer(self): pass
    with pytest.raises(ValueError):
        load_address(Dummy(), 0x10000)


# ---------------------------------------------------------------------------
# prog_multi
# ---------------------------------------------------------------------------

def test_prog_multi_frame(mock_serial):
    mock_serial.feed(bytes([INSYNC, OK]))
    prog_multi(mock_serial, b"\x01\x02\x03")
    assert bytes(mock_serial.written) == bytes([PROG_MULTI, 0x03, 0x01, 0x02, 0x03, EOC])


def test_prog_multi_rejects_empty(mock_serial):
    with pytest.raises((ValueError, UploadError)):
        prog_multi(mock_serial, b"")
    # No bytes should have been written.
    assert bytes(mock_serial.written) == b""


def test_prog_multi_rejects_too_long(mock_serial):
    with pytest.raises((ValueError, UploadError)):
        prog_multi(mock_serial, bytes(PROG_MULTI_MAX + 1))
    assert bytes(mock_serial.written) == b""


# ---------------------------------------------------------------------------
# upload_8051 (end-to-end)
# ---------------------------------------------------------------------------

def test_upload_8051_end_to_end(mock_serial):
    payload = bytes(range(70))  # 70 bytes -> 32 + 32 + 6
    image = _build_image([(0x1000, payload)])

    _install_auto(mock_serial)

    progress_calls: list[tuple[int, int]] = []
    board_id, freq_id = upload_8051(
        mock_serial,
        image,
        progress=lambda done, total: progress_calls.append((done, total)),
    )

    assert (board_id, freq_id) == (0x4E, 0x43)
    assert progress_calls == [(32, 70), (64, 70), (70, 70)]

    written = bytes(mock_serial.written)

    # CHIP_ERASE was issued.
    assert bytes([CHIP_ERASE, EOC]) in written
    # LOAD_ADDRESS for 0x1000.
    assert bytes([LOAD_ADDRESS, 0x00, 0x10, EOC]) in written
    # Final two bytes are REBOOT EOC.
    assert written.endswith(bytes([REBOOT, EOC]))


def test_upload_8051_unexpected_board_id(mock_serial):
    image = _build_image([(0x1000, b"\x00\x01\x02\x03")])
    _install_auto(mock_serial)
    with pytest.raises(UploadError):
        upload_8051(mock_serial, image, expected_board_id=0xFF)
    # No reboot should have been issued (mismatch happens before erase).
    assert not bytes(mock_serial.written).endswith(bytes([REBOOT, EOC]))


def test_upload_8051_cancel_after_one_prog_multi(mock_serial):
    payload = bytes(range(70))  # 3 prog_multi pieces total
    image = _build_image([(0x1000, payload)])
    _install_auto(mock_serial)

    state = {"prog_multi_count": 0}

    def cancel() -> bool:
        # Cancel kicks in after the first prog_multi has reported progress.
        return state["prog_multi_count"] >= 1

    progress_calls: list[tuple[int, int]] = []

    def progress(done, total):
        progress_calls.append((done, total))
        state["prog_multi_count"] += 1

    with pytest.raises(UploadCancelled):
        upload_8051(
            mock_serial,
            image,
            progress=progress,
            cancel_check=cancel,
        )

    # First piece succeeded; we reported (32, 70) before cancelling.
    assert progress_calls == [(32, 70)]
    # No REBOOT was written.
    assert bytes([REBOOT, EOC]) not in bytes(mock_serial.written)


def test_upload_8051_multiple_chunks(mock_serial):
    chunk_a = bytes(range(10))
    chunk_b = bytes(range(20, 25))
    # Two non-contiguous chunks so the parser keeps them separate.
    image = _build_image([(0x1000, chunk_a), (0x2000, chunk_b)])
    assert len(image.chunks) == 2

    _install_auto(mock_serial)

    progress_calls: list[tuple[int, int]] = []
    upload_8051(
        mock_serial,
        image,
        progress=lambda done, total: progress_calls.append((done, total)),
    )

    written = bytes(mock_serial.written)

    # Two LOAD_ADDRESS calls, one per chunk.
    la_a = bytes([LOAD_ADDRESS, 0x00, 0x10, EOC])
    la_b = bytes([LOAD_ADDRESS, 0x00, 0x20, EOC])
    assert written.count(la_a) == 1
    assert written.count(la_b) == 1
    # And they appear in chunk order.
    assert written.index(la_a) < written.index(la_b)

    # The PROG_MULTI frames carry the right data, in the right order.
    pm_a = bytes([PROG_MULTI, len(chunk_a)]) + chunk_a + bytes([EOC])
    pm_b = bytes([PROG_MULTI, len(chunk_b)]) + chunk_b + bytes([EOC])
    assert pm_a in written
    assert pm_b in written
    assert written.index(la_a) < written.index(pm_a) < written.index(la_b) < written.index(pm_b)

    # Total reported in progress equals image.total_bytes().
    total = image.total_bytes()
    assert total == len(chunk_a) + len(chunk_b)
    assert progress_calls[-1] == (total, total)
    assert written.endswith(bytes([REBOOT, EOC]))


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def test_chip_erase_writes_command(mock_serial):
    mock_serial.feed(bytes([INSYNC, OK]))
    chip_erase(mock_serial, timeout=0.1)
    assert bytes(mock_serial.written) == bytes([CHIP_ERASE, EOC])


def test_reboot_writes_no_reply_expected(mock_serial):
    reboot(mock_serial)
    assert bytes(mock_serial.written) == bytes([REBOOT, EOC])

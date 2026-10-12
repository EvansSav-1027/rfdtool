"""SiK 8051 bootloader uploader for RFD900 / RFD900+ / RFD900u radios.

Implements the small custom command/response protocol exposed by the SiK
bootloader (see ``Firmware/bootloaders/SiK`` in the upstream SiK firmware
repository). The protocol is *not* XMODEM: each command is a single byte
opcode followed by zero or more argument bytes terminated by ``EOC`` (0x20),
and each command receives an ``INSYNC`` (0x12) reply followed by either an
``OK`` (0x10), ``FAILED`` (0x11), or — for query commands — payload bytes
ending in ``OK``.

The radio is assumed to already be in the bootloader by the time the caller
hands a serial port to this module: the firmware processes ``AT&UPDATE`` and
jumps into the bootloader, which then emits a short banner before listening
for ``GET_SYNC``. ``get_sync`` flushes that banner via
:meth:`pyserial.Serial.reset_input_buffer` before its first probe.
"""
from __future__ import annotations

from typing import Callable, Protocol

from .ihx import HexImage


class UploadError(RuntimeError):
    """Bootloader protocol failure - desync, NAK, or unexpected board ID."""


class UploadCancelled(UploadError):
    """Caller's cancel_check returned True."""


class _SerialLike(Protocol):
    timeout: float | None

    def read(self, n: int = 1) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def reset_input_buffer(self) -> None: ...


# Bootloader command/response bytes (exposed for tests).
NOP          = 0x00
OK           = 0x10
FAILED       = 0x11
INSYNC       = 0x12
EOC          = 0x20
GET_SYNC     = 0x21
GET_DEVICE   = 0x22
CHIP_ERASE   = 0x23
LOAD_ADDRESS = 0x24
PROG_FLASH   = 0x25
READ_FLASH   = 0x26
PROG_MULTI   = 0x27
READ_MULTI   = 0x28
PARAM_ERASE  = 0x29
REBOOT       = 0x30

PROG_MULTI_MAX = 32  # bytes per PROG_MULTI command


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_byte(ser: _SerialLike, timeout: float) -> int:
    """Read one byte under the given timeout, raising UploadError on timeout."""
    saved = ser.timeout
    try:
        ser.timeout = timeout
        b = ser.read(1)
    finally:
        ser.timeout = saved
    if not b:
        raise UploadError(f"timeout waiting for byte (timeout={timeout})")
    return b[0]


def _expect_insync_ok(ser: _SerialLike, timeout: float) -> None:
    """Read the standard ``INSYNC OK`` ack and translate failures to UploadError."""
    a = _read_byte(ser, timeout)
    if a != INSYNC:
        raise UploadError(f"expected INSYNC (0x12), got 0x{a:02X}")
    b = _read_byte(ser, timeout)
    if b == FAILED:
        raise UploadError("bootloader returned FAILED")
    if b != OK:
        raise UploadError(f"expected OK (0x10), got 0x{b:02X}")


def _drain(ser: _SerialLike) -> None:
    """Drop any pending input. Best-effort; tolerates ports without the helper."""
    drain = getattr(ser, "reset_input_buffer", None)
    if drain is not None:
        drain()


# ---------------------------------------------------------------------------
# Low-level commands
# ---------------------------------------------------------------------------

def get_sync(ser: _SerialLike, *, timeout: float = 1.0, retries: int = 5) -> None:
    """Send ``GET_SYNC EOC``, expect ``INSYNC OK``. Retry on garbage/timeout.

    Drains any banner left over from ``AT&UPDATE`` before the first probe.
    Raises :class:`UploadError` after ``retries`` consecutive failures.
    """
    if retries < 1:
        raise ValueError("retries must be >= 1")

    _drain(ser)
    last_exc: Exception | None = None
    for _ in range(retries):
        try:
            ser.write(bytes([GET_SYNC, EOC]))
            _expect_insync_ok(ser, timeout)
            return
        except UploadError as exc:
            last_exc = exc
            _drain(ser)
            continue
    raise UploadError(
        f"GET_SYNC failed after {retries} attempts"
        + (f": {last_exc}" if last_exc is not None else "")
    )


def get_device(ser: _SerialLike, *, timeout: float = 1.0) -> tuple[int, int]:
    """Return ``(board_id, freq_id)`` from the bootloader's ``GET_DEVICE`` reply.

    The SiK bootloader sends the two payload bytes *first*, then
    ``INSYNC, OK`` (ArduPilot SiK ``Firmware/tools/uploader.py``:
    ``identify()`` does ``__recv(); __recv(); __getSync()``).
    """
    ser.write(bytes([GET_DEVICE, EOC]))
    board_id = _read_byte(ser, timeout)
    freq_id = _read_byte(ser, timeout)
    a = _read_byte(ser, timeout)
    if a != INSYNC:
        raise UploadError(f"expected INSYNC (0x12) after GET_DEVICE payload, got 0x{a:02X}")
    tail = _read_byte(ser, timeout)
    if tail == FAILED:
        raise UploadError("bootloader returned FAILED to GET_DEVICE")
    if tail != OK:
        raise UploadError(f"expected OK (0x10) after GET_DEVICE payload, got 0x{tail:02X}")
    return board_id, freq_id


def chip_erase(ser: _SerialLike, *, timeout: float = 20.0) -> None:
    """Erase the entire flash region. Slow - generous default timeout."""
    ser.write(bytes([CHIP_ERASE, EOC]))
    _expect_insync_ok(ser, timeout)


def load_address(ser: _SerialLike, address: int, *, timeout: float = 1.0) -> None:
    """Set the bootloader's program counter for the next PROG_MULTI / READ_MULTI."""
    if not 0 <= address <= 0xFFFF:
        raise ValueError(f"address out of 16-bit range: 0x{address:X}")
    lo = address & 0xFF
    hi = (address >> 8) & 0xFF
    ser.write(bytes([LOAD_ADDRESS, lo, hi, EOC]))
    _expect_insync_ok(ser, timeout)


def prog_multi(ser: _SerialLike, data: bytes, *, timeout: float = 2.0) -> None:
    """Program a 1..PROG_MULTI_MAX byte block at the current load address."""
    n = len(data)
    if n < 1:
        raise ValueError("prog_multi data must not be empty")
    if n > PROG_MULTI_MAX:
        raise ValueError(
            f"prog_multi data too long: {n} bytes (max {PROG_MULTI_MAX})"
        )
    frame = bytearray()
    frame.append(PROG_MULTI)
    frame.append(n)
    frame.extend(data)
    frame.append(EOC)
    ser.write(bytes(frame))
    _expect_insync_ok(ser, timeout)


def reboot(ser: _SerialLike) -> None:
    """Send ``REBOOT EOC``. The bootloader does not reply - it just jumps."""
    ser.write(bytes([REBOOT, EOC]))


# ---------------------------------------------------------------------------
# High-level upload
# ---------------------------------------------------------------------------

def _check_cancel(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise UploadCancelled("upload cancelled by caller")


def upload_8051(
    ser: _SerialLike,
    image: HexImage,
    *,
    expected_board_id: int | None = None,
    progress: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    sync_retries: int = 5,
) -> tuple[int, int]:
    """Flash ``image`` to the radio over the SiK 8051 bootloader.

    Sequence: GET_SYNC, GET_DEVICE, CHIP_ERASE, then for every chunk a single
    LOAD_ADDRESS followed by N PROG_MULTI calls of up to ``PROG_MULTI_MAX``
    bytes each. ``progress(bytes_done, total)`` fires after every PROG_MULTI;
    ``total`` is the payload byte count (``image.total_bytes()``), not the
    address span. Finally REBOOT.

    Raises :class:`UploadError` on protocol errors or unexpected board ID, and
    :class:`UploadCancelled` if ``cancel_check`` returns ``True`` between
    operations (no REBOOT is sent in that case).
    """
    total = image.total_bytes()
    # Refuse images this uploader can't address BEFORE anything is erased —
    # banked (>64 KiB) images need the 3-byte LOAD_ADDRESS form.
    for addr, data in image.chunks:
        if addr < 0 or addr + len(data) - 1 > 0xFFFF:
            raise UploadError(
                f"image has data at 0x{addr:X}, beyond 16-bit addressing "
                "(banked firmware is not supported) — nothing was erased"
            )

    _check_cancel(cancel_check)
    get_sync(ser, retries=sync_retries)

    _check_cancel(cancel_check)
    board_id, freq_id = get_device(ser)
    if expected_board_id is not None and board_id != expected_board_id:
        raise UploadError(
            f"unexpected board id: got 0x{board_id:02X}, "
            f"expected 0x{expected_board_id:02X}"
        )

    _check_cancel(cancel_check)
    chip_erase(ser)

    bytes_done = 0
    for addr, data in image.chunks:
        _check_cancel(cancel_check)
        load_address(ser, addr)

        offset = 0
        while offset < len(data):
            _check_cancel(cancel_check)
            piece = data[offset:offset + PROG_MULTI_MAX]
            prog_multi(ser, piece)
            offset += len(piece)
            bytes_done += len(piece)
            if progress is not None:
                progress(bytes_done, total)

    _check_cancel(cancel_check)
    reboot(ser)
    return board_id, freq_id

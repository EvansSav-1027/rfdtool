"""Flash ``.gbl`` firmware onto RFDesign x-series radios (RFD900x/RFD868x…).

Command-line replacement for the manual ``picocom`` + ``sx`` workflow.  Run
it through the main entry point::

    python3 rfdtool.py --port /dev/ttyUSB0 --file firmware.gbl [--yes]

Bootloader protocol (confirmed on an RFD868x v2):

* In bootloader mode (LED solid red) the radio answers a newline with a
  banner such as ``RFD900xSub:2:MainV1.12.03 ChipID: 980C33FFFE178A11``.
  It ignores AT commands and takes plain-text commands ending in CR LF.
* ``UPLOAD`` → ``Ready``, then repeated ``C`` (XMODEM-CRC requests).  The
  transfer must start within ~60 s, and each packet must follow its ACK
  within ~1 s.  Standard XMODEM-CRC, 128-byte blocks.
* ``BOOTNEW`` reboots into the new firmware.
* From normal firmware, ``+++`` (1 s guard silence either side) → ``OK``,
  then ``AT&UPDATE`` enters the bootloader.

Needs only ``pyserial`` and ``xmodem`` — no Qt, so it runs headless.
"""
from __future__ import annotations

import argparse
import csv
import errno
import hashlib
import io
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

BLOCK_SIZE = 128
DEFAULT_BAUD = 57600
MAX_ATTEMPTS = 3

CAN = b"\x18"
ACK = b"\x06"
NAK = b"\x15"
EOT = b"\x04"
CRC_REQ = b"C"

# Exit codes (one per failure type).
EXIT_OK = 0
EXIT_USAGE = 1          # bad arguments / missing or wrong file
EXIT_PORT = 2           # port missing, busy, or permission denied
EXIT_NO_BOOTLOADER = 3  # couldn't reach the bootloader
EXIT_DECLINED = 4       # user said no at the confirmation prompt
EXIT_UPLOAD = 5         # transfer failed after all attempts
EXIT_VERIFY = 6         # flashed, but the new firmware didn't answer
EXIT_INTERRUPTED = 130  # Ctrl-C

_BANNER = re.compile(r"(\S+)\s+ChipID:\s*([0-9A-Fa-f]+)")
# A complete banner: the ChipID is followed by a line end (it may arrive in
# pieces, so a bare match could be a truncated ID).
_BANNER_DONE = re.compile(r"ChipID:\s*[0-9A-Fa-f]+\s*[\r\n]")


class FlashError(RuntimeError):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


class RemoteCancel(RuntimeError):
    """The radio sent CAN CAN during the transfer."""


@dataclass(frozen=True)
class Banner:
    model: str
    chip_id: str
    raw: str


def parse_banner(text: str) -> Banner | None:
    """Find the bootloader banner (model string + ChipID) in `text`."""
    m = _BANNER.search(text)
    if not m:
        return None
    return Banner(model=m.group(1), chip_id=m.group(2).upper(), raw=m.group(0))


@dataclass
class Timing:
    """All waits in one place (tests shrink them)."""
    banner_read: float = 2.0       # read after sending a newline
    guard: float = 1.0             # +++ guard silence, each side
    ok_wait: float = 1.5           # wait for OK after +++
    bootloader_wait: float = 6.0   # banner after AT&UPDATE
    ready_wait: float = 5.0        # "Ready" after UPLOAD
    ack_timeout: float = 10.0      # per-packet ACK wait (lib default is 60 s)
    after_cancel: float = 1.0
    after_bootnew: float = 3.0
    ati_wait: float = 1.5


# --------------------------------------------------------------------- port I/O
class _Port:
    """Small helpers over a pyserial-like object."""

    def __init__(self, ser, timing: Timing, log: Callable[[str], None]) -> None:
        self.ser = ser
        self.t = timing
        self.log = log

    def flush_input(self) -> None:
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

    def send(self, data: bytes) -> None:
        self.ser.write(data)
        try:
            self.ser.flush()
        except Exception:
            pass

    def read_for(self, seconds: float, *, until: Callable[[str], bool] | None = None) -> str:
        """Read for `seconds`, or until ``until(text_so_far)`` is true."""
        deadline = time.monotonic() + seconds
        out = bytearray()
        while time.monotonic() < deadline:
            n = getattr(self.ser, "in_waiting", 0) or 1
            chunk = self.ser.read(n)
            if chunk:
                out.extend(chunk)
                if until is not None and until(out.decode("ascii", "replace")):
                    break
            else:
                time.sleep(0.01)
        return out.decode("ascii", "replace")

    def command(self, text: str, seconds: float, **kw) -> str:
        # CR LF for every command: CR alone was unreliable on the bootloader.
        self.send(text.encode("ascii") + b"\r\n")
        return self.read_for(seconds, **kw)


class _XmodemLink:
    """getc/putc for the ``xmodem`` library, with bootloader-aware fixes.

    * Before the first packet, skips the CR/LF that follows "Ready" so it
      doesn't eat into the start-sequence retry budget.  ``C`` bytes are
      passed straight through — the library must see them.
    * After a packet, a printable reply (bootloader text such as
      "NAK on sector") is read as a whole line, logged, and reported to the
      library as a single NAK.  Fed byte by byte it would count as many
      replies and desynchronise the transfer.
    * CAN CAN from the radio raises :class:`RemoteCancel` (the library would
      otherwise just keep retransmitting).
    """

    def __init__(self, port: _Port) -> None:
        self.port = port
        self.started = False
        self.last_can = False
        self.messages: list[str] = []
        self._pending = b""     # control byte found while reading a text line

    def getc(self, size: int, timeout: float = 1) -> bytes | None:
        ser = self.port.ser
        if self._pending:
            data, self._pending = self._pending, b""
        else:
            old = ser.timeout
            ser.timeout = timeout
            try:
                while True:
                    data = ser.read(size)
                    if not data:
                        return None
                    if not self.started and data in (b"\r", b"\n"):
                        continue          # line end after "Ready"
                    if self.started and data in (CRC_REQ, b"\r", b"\n"):
                        continue          # late 'C' request / stray line end
                    break
            finally:
                ser.timeout = old
        if data == CAN:
            if self.last_can:
                raise RemoteCancel("radio cancelled the transfer (CAN CAN)")
            self.last_can = True
            return data
        self.last_can = False
        if self.started and data not in (ACK, NAK) and 0x20 <= data[0] < 0x7F:
            line = (data + self._rest_of_line()).decode("ascii", "replace").strip()
            if line:
                self.messages.append(line)
                self.port.log(f"  radio: {line}")
            return NAK
        return data

    def _rest_of_line(self) -> bytes:
        ser = self.port.ser
        old = ser.timeout
        ser.timeout = 0.2
        out = bytearray()
        try:
            while len(out) < 200:
                b = ser.read(1)
                if not b or b == b"\n":
                    break
                if b[0] < 0x20 and b != b"\r":
                    self._pending = b    # ACK/NAK/CAN: the library's to see
                    break
                out += b
        finally:
            ser.timeout = old
        return bytes(out)

    def putc(self, data: bytes, timeout: float = 1) -> int:
        if len(data) > 1:
            self.started = True
        self.port.send(data)
        return len(data)


# --------------------------------------------------------------------- flasher
@dataclass
class FlashResult:
    banner: Banner | None = None
    ok: bool = False
    attempts: int = 0
    firmware: str = ""
    code: int = EXIT_OK
    detail: str = ""
    radio_messages: list[str] = field(default_factory=list)


class GblFlasher:
    def __init__(
        self,
        ser,
        *,
        timing: Timing | None = None,
        log: Callable[[str], None] = print,
        fw_baud: int | None = None,
    ) -> None:
        self.port = _Port(ser, timing or Timing(), log)
        self.t = self.port.t
        self.log = log
        self.boot_baud = ser.baudrate
        self.fw_baud = fw_baud or ser.baudrate

    # ---------------------------------------------------------- detection
    def read_banner(self) -> Banner | None:
        self.port.flush_input()
        text = self.port.command("", self.t.banner_read,
                                 until=lambda s: _BANNER_DONE.search(s) is not None)
        return parse_banner(text)

    def _at_mode(self) -> bool:
        """+++ with guard silence on both sides; True if OK came back."""
        self.port.flush_input()
        time.sleep(self.t.guard)
        self.port.send(b"+++")
        time.sleep(self.t.guard)
        reply = self.port.read_for(self.t.ok_wait, until=lambda s: "OK" in s)
        return "OK" in reply

    def _set_baud(self, baud: int) -> None:
        if self.port.ser.baudrate != baud:
            self.port.ser.baudrate = baud

    def firmware_info(self) -> str | None:
        """Non-destructive: ATI from normal firmware (then ATO), or None."""
        self._set_baud(self.fw_baud)
        try:
            if not self._at_mode():
                return None
            reply = self.port.command("ATI", self.t.ati_wait)
            self.port.command("ATO", 0.5)
            return _first_reply_line(reply, "ATI")
        finally:
            self._set_baud(self.boot_baud)

    def enter_bootloader(self) -> Banner | None:
        """From normal firmware: +++, AT&UPDATE, wait for the banner."""
        self._set_baud(self.fw_baud)
        try:
            if not self._at_mode():
                return None
            self.log("radio is running normal firmware — sending AT&UPDATE")
            self.port.send(b"AT&UPDATE\r\n")
        finally:
            self._set_baud(self.boot_baud)
        deadline = time.monotonic() + self.t.bootloader_wait
        while time.monotonic() < deadline:
            banner = self.read_banner()
            if banner:
                return banner
        return None

    def find_bootloader(self) -> Banner:
        banner = self.read_banner()
        if banner:
            return banner
        self.log("no bootloader banner — trying to enter the bootloader from firmware…")
        banner = self.enter_bootloader()
        if banner:
            return banner
        raise FlashError(
            "Couldn't reach the radio's bootloader (no banner, and no OK to +++).\n"
            "Force bootloader mode in hardware: power the radio off, ground pad 9 "
            "(see the RFD datasheet) while powering it on — the LED goes solid "
            "red — then run this again.",
            EXIT_NO_BOOTLOADER,
        )

    # ---------------------------------------------------------- transfer
    def upload(self, image: bytes, progress: Callable[[int, int, int], None] | None = None,
               ) -> tuple[bool, list[str]]:
        """One UPLOAD + XMODEM-CRC attempt.  Returns (ok, radio messages)."""
        from xmodem import XMODEM

        self.port.flush_input()
        self.port.send(b"UPLOAD\r\n")
        # Read only up to "Ready" — the 'C' requests that follow belong to
        # the XMODEM library.
        reply = self._read_until_token("Ready", self.t.ready_wait)
        if "Ready" not in reply:
            return False, [f"no 'Ready' after UPLOAD (got {reply.strip()!r})"]
        link = _XmodemLink(self.port)
        modem = XMODEM(link.getc, link.putc, mode="xmodem")
        total = math.ceil(len(image) / BLOCK_SIZE)

        def cb(_total_packets: int, success: int, errors: int) -> None:
            if progress is not None:
                progress(success, total, errors)

        try:
            ok = bool(modem.send(io.BytesIO(image), retry=16,
                                 timeout=self.t.ack_timeout, quiet=True, callback=cb))
        except RemoteCancel as e:
            link.messages.append(str(e))
            ok = False
        return ok, link.messages

    def _read_until_token(self, token: str, seconds: float) -> str:
        ser = self.port.ser
        deadline = time.monotonic() + seconds
        out = ""
        old = ser.timeout
        ser.timeout = 0.2
        try:
            while time.monotonic() < deadline:
                b = ser.read(1)          # byte by byte: stop right after the token
                if b:
                    out += b.decode("ascii", "replace")
                    if out.endswith(token):
                        break
        finally:
            ser.timeout = old
        return out

    def cancel_transfer(self) -> None:
        """Abort cleanly: CAN ×3, settle, flush.  The radio stays in its
        bootloader after a failed upload."""
        self.port.send(CAN * 3)
        time.sleep(self.t.after_cancel)
        self.port.flush_input()

    def boot_new(self) -> None:
        self.port.send(b"BOOTNEW\r\n")
        time.sleep(self.t.after_bootnew)

    def verify(self) -> str | None:
        return self.firmware_info()


def _first_reply_line(reply: str, echo: str) -> str | None:
    for line in reply.replace("\r", "\n").split("\n"):
        s = line.strip()
        if s and s.upper() not in ("OK", echo.upper()):
            return s
    return None


# --------------------------------------------------------------------- CLI
def _modemmanager_running() -> bool:
    try:
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    if Path(f"/proc/{pid}/comm").read_text().strip() == "ModemManager":
                        return True
                except OSError:
                    continue
    except OSError:
        pass
    return False


def open_port(port: str, baud: int):
    import serial

    if not os.path.exists(port):
        raise FlashError(f"{port} does not exist — is the radio plugged in?", EXIT_PORT)
    try:
        return serial.Serial(
            port, baud, bytesize=8, parity="N", stopbits=1, timeout=1,
            xonxoff=False, rtscts=False, dsrdtr=False, exclusive=True,
        )
    except serial.SerialException as e:
        msg = str(e)
        err = getattr(e, "errno", None)
        if err == errno.EACCES or "Permission denied" in msg:
            raise FlashError(
                f"Permission denied on {port}. Add yourself to the 'dialout' group:\n"
                "    sudo usermod -aG dialout $USER\n"
                "then log out and back in.", EXIT_PORT) from e
        if "lock" in msg.lower() or err == errno.EBUSY or "busy" in msg.lower():
            raise FlashError(
                f"{port} is in use by another program (rfdtool GUI, picocom, "
                "ModemManager…). Close it and try again.", EXIT_PORT) from e
        raise FlashError(f"Couldn't open {port}: {msg}", EXIT_PORT) from e


def _progress_printer(stream=sys.stderr) -> Callable[[int, int, int], None]:
    tty = stream.isatty()
    last = {"pct": -1}

    def show(done: int, total: int, errors: int) -> None:
        pct = int(100 * done / total) if total else 100
        if tty:
            width = 40
            filled = int(width * done / total) if total else width
            bar = "#" * filled + "-" * (width - filled)
            err = f"  retries:{errors}" if errors else ""
            stream.write(f"\r  [{bar}] {done}/{total} ({pct}%){err}   ")
            if done >= total:
                stream.write("\n")
            stream.flush()
        elif pct // 10 != last["pct"] // 10 or done >= total:
            last["pct"] = pct
            print(f"  {done}/{total} blocks ({pct}%)", file=stream, flush=True)

    return show


def _confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def flash_one(args, image: bytes, digest: str, *, log=print) -> FlashResult:
    result = FlashResult()
    ser = open_port(args.port, args.baud)
    try:
        flasher = GblFlasher(ser, log=log, fw_baud=args.fw_baud)
        if args.dry_run:
            banner = flasher.read_banner()
            if banner:
                result.banner = banner
                log(f"bootloader: {banner.model}   ChipID: {banner.chip_id}")
            else:
                fw = flasher.firmware_info()
                if fw is None:
                    raise FlashError("No bootloader banner and no reply to +++.",
                                     EXIT_NO_BOOTLOADER)
                result.firmware = fw
                log(f"radio is running firmware: {fw}")
                log("(dry run: not entering the bootloader)")
            result.ok = True
            return result

        banner = flasher.find_bootloader()
        result.banner = banner
        log(f"model:  {banner.model}")
        log(f"ChipID: {banner.chip_id}")
        if not args.yes and not _confirm(
            f"Flash {Path(args.file).name} onto this radio? A file for the wrong "
            "model can brick it."
        ):
            raise FlashError("Not flashed (declined).", EXIT_DECLINED)

        progress = _progress_printer()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            result.attempts = attempt
            log(f"uploading (attempt {attempt}/{MAX_ATTEMPTS})…")
            try:
                ok, messages = flasher.upload(image, progress)
            except KeyboardInterrupt:
                flasher.cancel_transfer()
                log("\ninterrupted — transfer cancelled; the radio is still in its bootloader")
                raise
            result.radio_messages.extend(messages)
            if ok:
                break
            log("\nupload failed" + (f": {messages[-1]}" if messages else ""))
            flasher.cancel_transfer()
            if not flasher.read_banner():
                log("  (no banner after cancel — retrying anyway)")
            if attempt < MAX_ATTEMPTS and not args.yes and not _confirm("Retry the upload?"):
                break
        else:
            ok = False
        if not ok:
            raise FlashError(
                f"Upload failed after {result.attempts} attempt(s). The radio is "
                "still in its bootloader — it's safe to run this again.", EXIT_UPLOAD)

        log("upload complete — booting new firmware (BOOTNEW)")
        flasher.boot_new()
        fw = flasher.verify()
        if fw is None:
            result.code = EXIT_VERIFY
            result.detail = "flashed, but the new firmware didn't answer +++/ATI"
            log(f"warning: {result.detail} (check --fw-baud)")
            return result
        result.firmware = fw
        result.ok = True
        log(f"new firmware: {fw}")
        return result
    finally:
        try:
            ser.close()
        except Exception:
            pass


_CSV_FIELDS = ["timestamp", "port", "model", "chip_id", "file", "sha256",
               "result", "attempts", "firmware", "detail"]


def _log_csv(path: str, args, digest: str, res: FlashResult) -> None:
    new = not Path(path).exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        if new:
            w.writeheader()
        w.writerow({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "port": args.port,
            "model": res.banner.model if res.banner else "",
            "chip_id": res.banner.chip_id if res.banner else "",
            "file": Path(args.file).name if args.file else "",
            "sha256": digest[:16],
            "result": "ok" if res.ok else f"fail({res.code})",
            "attempts": res.attempts,
            "firmware": res.firmware,
            "detail": res.detail or "; ".join(res.radio_messages[-3:]),
        })


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rfdtool.py",
        description="Flash .gbl firmware onto RFDesign x-series radios "
                    "(bootloader UPLOAD + XMODEM-CRC).",
    )
    p.add_argument("--port", required=True, help="serial port, e.g. /dev/ttyUSB0")
    p.add_argument("--file", help=".gbl firmware file (required unless --dry-run)")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD,
                   help="bootloader baud (default 57600)")
    p.add_argument("--fw-baud", type=int, default=None,
                   help="baud of the radio's normal firmware, for +++/AT&UPDATE "
                        "and the post-flash check (default: --baud)")
    p.add_argument("--yes", action="store_true",
                   help="don't ask for confirmation (or before retries)")
    p.add_argument("--dry-run", action="store_true",
                   help="detect the radio and print its info; don't flash")
    p.add_argument("--batch", action="store_true",
                   help="flash radio after radio, logging each to --log")
    p.add_argument("--log", default=None,
                   help="CSV log of each radio (default rfdtool_flash_log.csv with --batch)")
    return p


def _load_image(path: str | None, dry_run: bool) -> tuple[bytes, str]:
    if path is None:
        if dry_run:
            return b"", ""
        raise FlashError("--file is required (or use --dry-run).", EXIT_USAGE)
    f = Path(path)
    if f.suffix.lower() != ".gbl":
        raise FlashError(f"{f.name} isn't a .gbl file.", EXIT_USAGE)
    if not f.is_file():
        raise FlashError(f"{f} not found.", EXIT_USAGE)
    image = f.read_bytes()
    if not image:
        raise FlashError(f"{f.name} is empty.", EXIT_USAGE)
    return image, hashlib.sha256(image).hexdigest()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.batch and args.log is None:
        args.log = "rfdtool_flash_log.csv"
    try:
        image, digest = _load_image(args.file, args.dry_run)
    except FlashError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code
    if image:
        blocks = math.ceil(len(image) / BLOCK_SIZE)
        print(f"{Path(args.file).name}: {len(image):,} bytes, {blocks} blocks of {BLOCK_SIZE}")
    if _modemmanager_running():
        print("warning: ModemManager is running and may probe the port. Stop it "
              "(sudo systemctl stop ModemManager) if the transfer misbehaves.",
              file=sys.stderr)

    worst = EXIT_OK
    while True:
        try:
            res = flash_one(args, image, digest)
            code = res.code
        except FlashError as e:
            print(f"error: {e}", file=sys.stderr)
            res = FlashResult(code=e.code, detail=str(e).splitlines()[0])
            code = e.code
        except KeyboardInterrupt:
            return EXIT_INTERRUPTED
        if args.log and not args.dry_run:
            _log_csv(args.log, args, digest, res)
        if code != EXIT_OK and worst == EXIT_OK:
            worst = code
        if not args.batch:
            return code
        try:
            more = input("\nConnect the next radio and press Enter (q + Enter to finish): ")
        except (EOFError, KeyboardInterrupt):
            more = "q"
        if more.strip().lower().startswith("q"):
            if args.log:
                print(f"log: {args.log}")
            return worst


if __name__ == "__main__":
    raise SystemExit(main())

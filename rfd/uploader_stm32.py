"""STM32 system-memory bootloader uploader for RFD900x / RFD900ux radios.

These radios use an STM32 MCU whose factory-burned ROM bootloader speaks the
ST USART protocol over the radio's serial port. We don't reimplement that
protocol here - instead, we drive the well-tested ``stm32flash`` CLI as a
subprocess and parse its line-buffered stdout to drive a progress callback.

This module assumes the radio is **already in bootloader mode** by the time
``upload_stm32`` is invoked. The two ways to get it there are out of scope:

* On STM32 SiK builds, the firmware processes ``AT&UPDATE`` and jumps into
  system memory.
* On bare hardware, the BOOT/CTS pin must be held to ground while the radio
  powers up.

Either way, the caller hands us a serial port path that the ROM bootloader
is listening on, plus a ``.bin`` firmware file, and we run::

    stm32flash -b 57600 -e 0xff -w fw.bin -v -g 0x08000000 -S 0x08000000 PORT

and translate ``stm32flash``'s output into ``progress(done, total)`` calls.

The module is stdlib-only.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class STM32FlashError(RuntimeError):
    """stm32flash exited non-zero, or its output indicated an error."""


class STM32FlashMissing(STM32FlashError):
    """stm32flash binary not found on PATH."""


class STM32FlashCancelled(STM32FlashError):
    """User cancelled mid-flash."""


# ---------------------------------------------------------------------------
# Availability / hint helpers
# ---------------------------------------------------------------------------

def stm32flash_available(binary: str = "stm32flash") -> bool:
    """Return ``True`` if ``binary`` resolves on ``$PATH``."""
    return shutil.which(binary) is not None


def stm32flash_install_hint() -> str:
    """Return a multi-line install hint mentioning the apt package and project link."""
    return (
        "stm32flash is required to flash RFD900x / RFD900ux radios.\n"
        "Install it with:  sudo apt install stm32flash\n"
        "Project page: https://sourceforge.net/projects/stm32flash/"
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Matches any number with an optional decimal followed by '%' (e.g. "12.34%",
# "100 %", "5%"). We extract the numeric portion as a float.
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")

# How many recent stdout lines to keep for inclusion in error messages on a
# non-zero exit. stm32flash failures usually print the diagnostic immediately
# before exit, so a short tail is plenty.
_TAIL_KEEP = 50
_TAIL_REPORT = 10


def _build_args(
    binary: str,
    port: str,
    bin_path: str,
    *,
    baud: int,
    start_address: int,
    verify: bool,
    erase: bool,
    extra_args: list[str] | None,
) -> list[str]:
    """Assemble the stm32flash argv for the given options.

    Order matters only for the trailing positional ``port``; the rest of the
    flags are independent. We prepend ``-e 0xff`` when ``erase`` is true so
    stm32flash erases up to 255 pages before the write (effectively a chip
    erase for the F1-class parts on RFD900x/ux).
    """
    args: list[str] = [binary, "-b", str(baud)]
    if erase:
        # ``-e n`` tells stm32flash to erase n pages prior to write. 0xff is
        # larger than any page count we'll see on these MCUs, so it's a
        # convenient "erase everything we're about to touch" stand-in.
        args += ["-e", "0xff"]
    args += ["-w", bin_path]
    if verify:
        args += ["-v"]
    args += ["-g", f"0x{start_address:08X}"]
    args += ["-S", f"0x{start_address:08X}"]
    if extra_args:
        args += list(extra_args)
    args += [port]
    return args


def _default_factory(args: list[str]) -> Any:
    """Spawn stm32flash with line-buffered, merged stdout/stderr text streams."""
    return subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def upload_stm32(
    port: str,
    bin_path: str,
    *,
    progress: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    log: Callable[[str], None] | None = None,
    baud: int = 57600,
    start_address: int = 0x08000000,
    binary: str = "stm32flash",
    verify: bool = True,
    erase: bool = True,
    extra_args: list[str] | None = None,
    subprocess_factory: Callable[[list[str]], Any] | None = None,
) -> None:
    """Flash ``bin_path`` to the radio on ``port`` via the stm32flash CLI.

    The radio is assumed to already be sitting in its STM32 system-memory
    bootloader. We invoke ``stm32flash`` as a subprocess, stream its stdout
    line-by-line, and translate progress lines like
    ``Wrote address 0x08001000 (12.34%)`` into ``progress(done, total)``
    callbacks. ``total`` is taken from ``os.path.getsize(bin_path)``.

    Cancellation is cooperative: when ``cancel_check`` returns truthy between
    output lines, we ``terminate()`` the subprocess, wait briefly for it to
    exit, and raise :class:`STM32FlashCancelled`. Non-zero exit raises
    :class:`STM32FlashError` with the last few lines of merged stdout/stderr
    appended to help diagnose the failure.

    Parameters mirror the spec in the project's docs; see ``_build_args`` for
    how the flags map onto the ``stm32flash`` CLI.

    The ``subprocess_factory`` hook is for tests and accepts the full argv
    list; it must return an object with a ``stdout.readline()`` method, plus
    ``poll()``, ``wait()``, and ``terminate()`` methods compatible with
    :class:`subprocess.Popen`.
    """
    # When tests provide their own subprocess factory we skip the PATH check:
    # there is no real binary to find. In production, the missing-binary case
    # is the most common failure mode and deserves a tailored exception so
    # the GUI can offer the install hint.
    if subprocess_factory is None and not stm32flash_available(binary):
        raise STM32FlashMissing(stm32flash_install_hint())

    if not os.path.exists(bin_path):
        raise STM32FlashError(f"firmware file not found: {bin_path}")

    total = os.path.getsize(bin_path)
    args = _build_args(
        binary,
        port,
        bin_path,
        baud=baud,
        start_address=start_address,
        verify=verify,
        erase=erase,
        extra_args=extra_args,
    )

    factory = subprocess_factory or _default_factory
    proc = factory(args)

    last_pct = -1.0
    tail: list[str] = []

    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                # No data available right now. If the process is gone we're
                # done; otherwise check for cancel and loop again.
                if proc.poll() is not None:
                    break
                if cancel_check is not None and cancel_check():
                    proc.terminate()
                    try:
                        proc.wait(timeout=2.0)
                    except Exception:
                        # ``wait`` may raise TimeoutExpired on a real Popen,
                        # but a stuck flasher isn't going to come back.
                        pass
                    raise STM32FlashCancelled("user cancelled")
                time.sleep(0.02)   # EOF before exit: don't spin a core
                continue

            line = line.rstrip("\r\n")
            if log is not None:
                log(line)

            tail.append(line)
            if len(tail) > _TAIL_KEEP:
                # Trim from the front so we always have the most recent
                # _TAIL_KEEP lines available for the error tail.
                del tail[: len(tail) - _TAIL_KEEP]

            m = _PCT_RE.search(line)
            if m is not None:
                pct = float(m.group(1))
                # Only forward jumps drive progress: stm32flash sometimes
                # restarts a phase (write -> verify) which would otherwise
                # cause the bar to go backwards.
                if pct > last_pct:
                    last_pct = pct
                    if progress is not None:
                        done = int(total * pct / 100.0)
                        progress(min(done, total), total)

            if cancel_check is not None and cancel_check():
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
                raise STM32FlashCancelled("user cancelled")

        rc = proc.wait()
        if rc != 0:
            tail_text = "\n".join(tail[-_TAIL_REPORT:])
            raise STM32FlashError(f"stm32flash exited {rc}: {tail_text}")

        # If the stream ended before any 100% line (e.g. stm32flash printed
        # only "Done." without a final percentage), still report a clean
        # finish so progress bars don't stick at 99%.
        if progress is not None and last_pct < 100.0:
            progress(total, total)
    finally:
        # Best-effort cleanup; ignore double-close or already-detached pipes.
        stdout = getattr(proc, "stdout", None)
        if stdout is not None:
            try:
                stdout.close()
            except Exception:
                pass

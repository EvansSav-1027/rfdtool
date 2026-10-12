"""Serial port enumeration and radio fingerprinting (SiK and MultiComm).

`list_serial_ports()` enumerates all USB serial adapters present, with VID/PID
where available so the UI can highlight likely RFD900 candidates (FTDI FT231X,
Silicon Labs CP210x).

`fingerprint_port()` opens a port at the requested baud, tries to enter
command mode, runs the identification AT commands, and returns what it found
(or None if no SiK radio responds).  Designed so the UI can sweep every port
in parallel via QtConcurrent or a thread pool.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import protocol as proto
from .radio import RadioCore, default_serial_factory


# VID/PIDs of USB-serial chips commonly shipped with RFD900-series modems.
# Not exhaustive — the UI uses these as a hint, not a hard filter.
KNOWN_USB_VID_PIDS: dict[tuple[int, int], str] = {
    (0x0403, 0x6001): "FTDI FT232",
    (0x0403, 0x6015): "FTDI FT231X",     # very common on RFDesign breakouts
    (0x0403, 0x6011): "FTDI FT4232H",
    (0x10C4, 0xEA60): "Silicon Labs CP210x",
    (0x1A86, 0x7523): "QinHeng CH340",
}


@dataclass(frozen=True)
class PortInfo:
    device: str
    description: str
    vid: int | None
    pid: int | None
    serial_number: str | None
    manufacturer: str | None
    likely_radio: bool

    def usb_label(self) -> str:
        if self.vid is None or self.pid is None:
            return ""
        name = KNOWN_USB_VID_PIDS.get((self.vid, self.pid))
        return name or f"USB {self.vid:04x}:{self.pid:04x}"


def list_serial_ports() -> list[PortInfo]:
    """Enumerate all serial ports on the system."""
    from serial.tools import list_ports

    out: list[PortInfo] = []
    for p in list_ports.comports():
        vid = getattr(p, "vid", None)
        pid = getattr(p, "pid", None)
        likely = (vid, pid) in KNOWN_USB_VID_PIDS if vid and pid else False
        out.append(
            PortInfo(
                device=p.device,
                description=p.description or "",
                vid=vid,
                pid=pid,
                serial_number=getattr(p, "serial_number", None),
                manufacturer=getattr(p, "manufacturer", None),
                likely_radio=likely,
            )
        )
    out.sort(key=lambda x: (not x.likely_radio, x.device))
    return out


@dataclass(frozen=True)
class RadioFingerprint:
    port: str
    baud: int
    banner: str
    board_id: int | None
    board_name: str
    freq_id: int | None
    bootloader_version: str
    linked: bool   # True if the remote radio also responded
    firmware_family: str = "sik"   # rfd.registers.FAMILY_*


def fingerprint_port(
    port: str,
    baud: int = 57600,
    *,
    serial_factory=None,
    bracket: proto.CommandModeBracket | None = None,
) -> RadioFingerprint | None:
    """Try to identify a SiK radio on `port`.  Returns None if nothing responds.

    Closes the serial port before returning either way.
    """
    factory = serial_factory or default_serial_factory
    ser = None
    try:
        ser = factory(port, baud, 0.1)
        core = RadioCore(ser)
        if not core.enter_command_mode(bracket):
            return None
        info = core.identify(timeout=1.0)
        # Probe for a linked remote — short timeout.
        try:
            linked = core.probe_remote(timeout=0.5)
        except Exception:
            linked = False
        try:
            core.exit_command_mode()
        except Exception:
            pass
        return RadioFingerprint(
            port=port,
            baud=baud,
            banner=str(info.get("banner") or ""),
            board_id=info.get("board_id"),  # type: ignore[arg-type]
            board_name=str(info.get("board_name") or ""),
            freq_id=info.get("freq_id"),  # type: ignore[arg-type]
            bootloader_version=str(info.get("bootloader_version") or ""),
            linked=linked,
            firmware_family=core.family,
        )
    except Exception:
        return None
    finally:
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass


# Common SiK/MultiComm baud rates in the order most likely to be in use
# today.  The high rates at the end are MultiComm-only (S1BAUD up to 1.5M).
COMMON_BAUDS: tuple[int, ...] = (
    57600, 115200, 38400, 19200, 9600, 4800, 2400, 230400,
    460800, 1000000, 1500000, 1200000,
)


def fingerprint_port_autobaud(
    port: str,
    *,
    serial_factory=None,
    bracket: proto.CommandModeBracket | None = None,
    bauds: tuple[int, ...] = COMMON_BAUDS,
) -> RadioFingerprint | None:
    """Try every baud in `bauds` until one fingerprints successfully."""
    for baud in bauds:
        fp = fingerprint_port(port, baud, serial_factory=serial_factory, bracket=bracket)
        if fp is not None:
            return fp
    return None

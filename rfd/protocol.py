"""AT command builders and ATI/ATI5/ATI7 response parsers.

Covers SiK firmware and RFDesign MultiComm (see :mod:`rfd.multicomm`).

The radio listens for AT commands while in *command mode* (entered with the
+++ escape sequence and exited with ATO).  RT-prefixed commands are forwarded
to the linked remote radio — on MultiComm they're broadcast to every node
unless suffixed with ``@<NODEID>``.  ATI5 enumerates all S-registers (MultiComm
adds ``ATI5:x:y`` sub-ranges); ATI7 reports RSSI/noise/error counters on SiK;
ATI/ATI2/ATI3/ATI4 give the firmware banner, board ID, board frequency
identifier, and bootloader version respectively.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .registers import FAMILY_SIK, ParamMeta, ParamValue, coerce_value, format_value

# --------------------------------------------------------------------- board IDs
# Best-effort table.  The UI falls back to "Unknown (ID 0xNN)" if absent.
BOARD_IDS: dict[int, str] = {
    0x4D: "RFD900a",
    0x4E: "RFD900",
    0x4F: "RFD900u",
    0x50: "RFD900p",   # RFD900+
    0x7B: "RFD900x",
    0x7D: "RFD900ux",
    0x84: "RFD900x2",  # newer X2 (SiK 3.x); confirmed on RFD900X2-US hardware
}

FREQ_IDS: dict[int, str] = {
    0x43: "433 MHz",
    0x86: "868 MHz",
    0x91: "915 MHz",
}


def board_name(board_id: int | None) -> str:
    if board_id is None:
        return "Unknown"
    return BOARD_IDS.get(board_id, f"Unknown (ID 0x{board_id:02X})")


def is_stm32_board(board_id: int | None) -> bool:
    """RFD900x / RFD900ux / RFD900x2 are STM32-based; rest are 8051."""
    return board_id in (0x7B, 0x7D, 0x84)


# --------------------------------------------------------------------- AT commands
def _target(remote: bool, node: int | None) -> str:
    """MultiComm ``@NODEID`` suffix for a remote command (empty = broadcast)."""
    return f"@{int(node)}" if remote and node is not None else ""


def at_set_param(
    sreg: int, value: ParamValue, *, remote: bool = False, node: int | None = None,
) -> bytes:
    prefix = "RT" if remote else "AT"
    target = _target(remote, node)
    text = format_value(value)
    if target:
        # Avoid ",@2": the manual shows arrays both with a trailing comma
        # (ATS48=K) and without (ATS47=1000,1100,1200).
        text = text.rstrip(",")
    return f"{prefix}S{sreg}={text}{target}\r\n".encode("ascii")


def at_set_pin(pin: int, value: int, *, remote: bool = False) -> bytes:
    prefix = "RT" if remote else "AT"
    return f"{prefix}R{pin}={value}\r\n".encode("ascii")


def at_save_eeprom(*, remote: bool = False) -> bytes:
    return b"RT&W\r\n" if remote else b"AT&W\r\n"


def at_reboot(*, remote: bool = False, node: int | None = None) -> bytes:
    return f"RTZ{_target(True, node)}\r\n".encode() if remote else b"ATZ\r\n"


def at_factory_reset(*, remote: bool = False, node: int | None = None) -> bytes:
    return f"RT&F{_target(True, node)}\r\n".encode() if remote else b"AT&F\r\n"


def at_read_params(
    *,
    remote: bool = False,
    node: int | None = None,
    first: int | None = None,
    last: int | None = None,
) -> bytes:
    """``ATI5`` / ``RTI5``, optionally limited to S``first``..S``last``
    inclusive (MultiComm ``ATI5:x:y``) and addressed to one remote node."""
    prefix = "RTI5" if remote else "ATI5"
    rng = f":{int(first)}:{int(last)}" if first is not None and last is not None else ""
    return f"{prefix}{rng}{_target(remote, node)}\r\n".encode("ascii")


def at_rssi() -> bytes:
    return b"ATI7\r\n"


def at_param_info(sreg: int) -> bytes:
    """MultiComm ``ATI10:n`` — one register with its valid range/options."""
    return f"ATI10:{int(sreg)}\r\n".encode("ascii")


def at_rssi_report(enable: bool) -> bytes:
    """MultiComm/SiK debug RSSI report on (``AT&T=RSSI``) or off (``AT&T``)."""
    return b"AT&T=RSSI\r\n" if enable else b"AT&T\r\n"


def at_remote_identify(*, node: int | None = None) -> bytes:
    """``RTI`` — every remote in range (or just ``node``) replies with its banner."""
    return f"RTI{_target(True, node)}\r\n".encode("ascii")


# --------------------------------------------------------------------- replies
_ERROR_RE = re.compile(r"\bERROR([23]?)\b")


def reply_error(text: str) -> str | None:
    """Return ``"ERROR"``, ``"ERROR2"`` or ``"ERROR3"`` if present in `text`.

    MultiComm (§3.4): ERROR = invalid command, ERROR2 = invalid parameter
    value, ERROR3 = invalid command issued remotely.  SiK only uses ERROR.
    """
    m = _ERROR_RE.search(text.upper())
    return f"ERROR{m.group(1)}" if m else None


ERROR_MEANINGS: dict[str, str] = {
    "ERROR": "command not recognised",
    "ERROR2": "value rejected (out of range or locked)",
    "ERROR3": "remote node rejected the command",
}


def at_bootloader() -> bytes:
    return b"AT&UPDATE\r\n"


def at_exit_command_mode() -> bytes:
    return b"ATO\r\n"


def at_identify() -> bytes:
    return b"ATI\r\n"


def at_board_id() -> bytes:
    return b"ATI2\r\n"


def at_freq_id() -> bytes:
    return b"ATI3\r\n"


def at_bootloader_version() -> bytes:
    return b"ATI4\r\n"


# --------------------------------------------------------------------- ATI5 parser
# One register per line.  Plain SiK form::
#
#   S1: SERIAL_SPEED=57
#
# MultiComm decorated form (ATI10:n; possibly ATI5 on some builds)::
#
#   S1:BITRATE(N)[56..200]=200 {12,56,64,72,100,125,188,200,430,1000,2000,}
#   S35:PIN0TYPE(13)(L)[0..30]{0x1000007F}=1 {GPI,RESET,MIRROR,…,}
#   S45:PINDEFAULT(A)[0..1]{8}=1,1,0,0,0,0,0,0,
#   S33:RSVD1(N)[0..110]<-1.00,0.00>=0
#
# Names may contain `/` (RFDesign 3.x ``GPI1_1R/CIN``, MultiComm
# ``RATE/FREQBAND``) and a pin annotation ``(13)`` which stays part of the
# name.  Values are a signed integer or a comma-separated array.  The tag
# may follow a prefix (remote replies) but not be glued to a word.
_ATI5_VALUE = r"(?P<value>0x[0-9a-fA-F]+|-?\d+(?:\s*,\s*-?\d+)*\s*,?)"
_ATI5_DECOR = (
    r"(?P<name>[^\s=(\[{<:]+(?:\(\d+\))?)"
    r"(?:\((?P<type>[A-Za-z])\))?"
    r"(?:\[(?P<min>-?\d+)\.\.(?P<max>-?\d+)\])?"
    r"(?:\{(?P<mask>[^}]*)\})?"
    r"(?:<[^>]*>)?"
)
_ATI5_TAIL = r"(?:\s*\{(?P<opts>[^}]*)\})?\s*$"
ATI5_LINE = re.compile(
    r"(?<![A-Za-z0-9])S(?P<sreg>\d+)\s*:\s*" + _ATI5_DECOR + r"\s*=\s*"
    + _ATI5_VALUE + _ATI5_TAIL,
    re.IGNORECASE,
)
ATI5_PIN_LINE = re.compile(
    r"(?<![A-Za-z0-9])R(?P<sreg>\d+)\s*:\s*" + _ATI5_DECOR + r"\s*=\s*"
    + _ATI5_VALUE + _ATI5_TAIL,
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Ati5Result:
    """Parsed ATI5 output.

    ``s_params`` / ``pin_params`` map sreg → value as before. ``s_names`` /
    ``pin_names`` map sreg → the parameter name the firmware reported, so
    callers can build a name-keyed view of the configuration without
    assuming the canonical SiK layout.  ``s_meta`` holds radio-reported
    constraints for registers printed in MultiComm's decorated form.
    """
    s_params: dict[int, ParamValue]
    pin_params: dict[int, ParamValue]
    s_names: dict[int, str]
    pin_names: dict[int, str]
    # Firmware family the reply came from (rfd.registers.FAMILY_*).  Set by
    # RadioCore.read_params; parse_ati5 alone can't tell.
    family: str = FAMILY_SIK
    s_meta: dict[int, ParamMeta] = field(default_factory=dict)


def _parse_value(text: str) -> ParamValue:
    # A comma means an array even with one element ("5,").
    if "," in text:
        return coerce_value(text)
    return int(text, 16) if text[:2].lower() == "0x" else int(text)


def _parse_meta(m: re.Match) -> ParamMeta | None:
    type_code = (m.group("type") or "").upper()
    if not type_code and m.group("min") is None:
        return None   # plain line — nothing reported
    mask = length = None
    raw_mask = (m.group("mask") or "").strip()
    if raw_mask:
        if raw_mask.lower().startswith("0x"):
            mask = int(raw_mask, 16)
        elif raw_mask.isdigit():
            length = int(raw_mask)
    opts = tuple(o.strip() for o in (m.group("opts") or "").split(",") if o.strip())
    return ParamMeta(
        type_code=type_code,
        minimum=int(m.group("min")) if m.group("min") is not None else None,
        maximum=int(m.group("max")) if m.group("max") is not None else None,
        mask=mask,
        length=length,
        options=opts,
    )


def parse_ati5(text: str) -> Ati5Result:
    """Parse ATI5 output into S-register and R-register dicts (with names).

    Tolerates the echoed command, blank lines, mixed line endings, section
    separators, and the trailing prompt.  Lines that don't match either
    pattern are ignored.
    """
    s_params: dict[int, ParamValue] = {}
    pin_params: dict[int, ParamValue] = {}
    s_names: dict[int, str] = {}
    pin_names: dict[int, str] = {}
    s_meta: dict[int, ParamMeta] = {}
    for line in text.replace("\r", "").splitlines():
        m = ATI5_LINE.search(line)
        if m:
            sreg = int(m.group("sreg"))
            s_params[sreg] = _parse_value(m.group("value"))
            s_names[sreg] = m.group("name")
            meta = _parse_meta(m)
            if meta is not None:
                s_meta[sreg] = meta
            continue
        m = ATI5_PIN_LINE.search(line)
        if m:
            sreg = int(m.group("sreg"))
            pin_params[sreg] = _parse_value(m.group("value"))
            pin_names[sreg] = m.group("name")
    return Ati5Result(
        s_params=s_params,
        pin_params=pin_params,
        s_names=s_names,
        pin_names=pin_names,
        s_meta=s_meta,
    )


# --------------------------------------------------------------------- ATI7 parser
@dataclass(frozen=True)
class NodeRssi:
    """One remote node's line(s) from a MultiComm ``AT&T=RSSI`` report (dBm)."""
    node: int
    local_rssi: float | None = None     # how strongly we hear `node`
    remote_rssi: float | None = None    # how strongly `node` hears its peers
    remote_noise: float | None = None   # noise floor at `node`


@dataclass(frozen=True)
class RssiReport:
    local_rssi: float | None = None
    remote_rssi: float | None = None
    local_noise: float | None = None
    remote_noise: float | None = None
    pkts: int | None = None
    txe: int | None = None
    rxe: int | None = None
    stx: int | None = None
    srx: int | None = None
    ecc_corrected: int | None = None
    ecc_uncorrected: int | None = None
    temp_c: int | None = None
    raw: str = ""
    # "raw" = SiK 0..255 RSSI units; "dBm" = MultiComm report (negative dBm).
    units: str = "raw"
    # True/False when the reply proves the link is up/down; None = can't tell.
    linked: bool | None = None
    nodes: tuple[NodeRssi, ...] = ()


def parse_ati7(text: str) -> RssiReport:
    """Best-effort parse of the ATI7 RSSI/diagnostic line.

    Typical format::

      L/R RSSI: 222/216  L/R noise: 30/29 pkts: 12345  txe=0 rxe=0 stx=0 srx=0 ecc=0/0 temp=-273

    Firmware variants reorder fields and sometimes omit some; we extract
    everything we recognise and leave the rest as None.
    """
    raw = text.strip()
    blob = raw.replace("\r", " ").replace("\n", " ")

    def grab(pattern: str) -> tuple[int, ...] | None:
        m = re.search(pattern, blob, re.IGNORECASE)
        if not m:
            return None
        return tuple(int(g) for g in m.groups())

    lr = grab(r"L/R\s+RSSI\s*:\s*(\d+)\s*/\s*(\d+)")
    ln = grab(r"L/R\s+noise\s*:\s*(\d+)\s*/\s*(\d+)")
    pk = grab(r"pkts\s*:\s*(\d+)")
    txe = grab(r"\btxe\s*=\s*(\d+)")
    rxe = grab(r"\brxe\s*=\s*(\d+)")
    stx = grab(r"\bstx\s*=\s*(\d+)")
    srx = grab(r"\bsrx\s*=\s*(\d+)")
    ecc = grab(r"\becc\s*=\s*(\d+)\s*/\s*(\d+)")
    tmp = grab(r"\btemp\s*=\s*(-?\d+)")

    linked: bool | None = None
    if pk is not None and lr is not None:
        linked = pk[0] > 0 and lr[1] > 0

    return RssiReport(
        local_rssi=lr[0] if lr else None,
        remote_rssi=lr[1] if lr else None,
        local_noise=ln[0] if ln else None,
        remote_noise=ln[1] if ln else None,
        pkts=pk[0] if pk else None,
        txe=txe[0] if txe else None,
        rxe=rxe[0] if rxe else None,
        stx=stx[0] if stx else None,
        srx=srx[0] if srx else None,
        ecc_corrected=ecc[0] if ecc else None,
        ecc_uncorrected=ecc[1] if ecc else None,
        temp_c=tmp[0] if tmp else None,
        raw=raw,
        linked=linked,
    )


# --------------------------------------------------------------------- single-line parsers
def parse_banner(text: str) -> str:
    """Pull the firmware banner from an ATI response.

    SiK echoes "ATI" then prints the banner; we return the last non-empty,
    non-OK line that doesn't start with "AT" (i.e. isn't an echoed command).
    """
    for line in reversed(text.replace("\r", "").splitlines()):
        s = line.strip()
        if not s:
            continue
        if s.upper() == "OK":
            continue
        if s.upper().startswith("AT"):
            continue
        return s
    return text.strip()


def parse_int_response(text: str) -> int | None:
    """Pick the first integer (decimal or 0x-prefixed hex) from a response."""
    for line in text.replace("\r", "").splitlines():
        s = line.strip()
        if not s or s.upper() == "OK" or s.upper().startswith("AT"):
            continue
        if s.isdigit():
            return int(s)
        if re.fullmatch(r"-?\d+", s):
            return int(s)
        m = re.fullmatch(r"0x([0-9a-fA-F]+)", s)
        if m:
            return int(m.group(1), 16)
    return None


# --------------------------------------------------------------------- +++ sequencer
@dataclass(frozen=True)
class CommandModeBracket:
    """Timing parameters for entering command mode.

    SiK requires ~1 second of UART silence on either side of the literal
    "+++" with no CR/LF.  The actual sleeps are done by the caller; this
    struct is just the configuration the caller pulls from.
    """
    quiet_before: float = 1.1
    plus_string: bytes = b"+++"
    quiet_after: float = 1.1
    expected_reply: bytes = b"OK"
    reply_timeout: float = 2.0

"""RFDesign MultiComm firmware (v4.x) — parameter catalog and helpers.

MultiComm is RFDesign's point-to-point / multipoint / mesh firmware for V2
hardware (RFD900x2, RFD900ux2, RFD868x2, RFD868ux2).  It is **not**
interoperable with SiK and uses a completely different S-register map:
S0..S49, several of which are comma-separated arrays (S45..S49) and one of
which (S9 SLOTIDS) is a 64-bit bitmask.

Source: *RFD MultiComm Firmware User Manual v1.0* (20/02/2025),
https://files.rfdesign.com.au/Files/documents/MultiCommFirmwareUserManual1.0.pdf
— §3 (AT/RT commands), §4 (S-parameter table and definitions), §8.2 (RSSI
report).  Section references in comments below point into that manual.

Protocol differences from SiK that the rest of rfdtool has to honour:

* Parameters are persisted on every ``ATSn=`` write — there is no ``AT&W``.
  A reboot (``ATZ``) is still required for most changes to take effect.
* ``ATI5`` output may be truncated by the UART buffer; ``ATI5:x:y`` reads
  an inclusive sub-range and is what :class:`rfd.radio.RadioCore` uses.
* ``RT`` commands are broadcast to every node in range unless suffixed with
  ``@<NODEID>`` (e.g. ``RTI5@2``).
* ``ATI7`` is TDM timing, not RSSI.  RSSI comes from the ``AT&T=RSSI``
  debug report (:func:`parse_rssi_report`).
* Errors are ``ERROR`` (bad command), ``ERROR2`` (bad parameter value) and
  ``ERROR3`` (bad remote command).
"""
from __future__ import annotations

import re

from .registers import FAMILY_MULTICOMM, ParamSpec, ParamValue

# --------------------------------------------------------------------- enums
# Air-rate option list reported by v4.00 firmware (ATI10:1).  The manual's
# §8 table omits 72 and 2000.  Which of these a given radio accepts is
# region-dependent and comes from its reported range at runtime.
BITRATES: tuple[int, ...] = (12, 56, 64, 72, 100, 125, 188, 200, 430, 1000, 2000)

# S1BAUD/S2BAUD option list reported by v4.00 firmware (ATI10:21) — note
# no 19200/38400 despite the manual's §8 list.
SERIAL_BAUDS: tuple[int, ...] = (
    9600, 57600, 115200, 230400, 460800, 1000000, 1200000, 1500000,
)

_DATATYPE_ENUM = {0: "MAVLink 1/2", 1: "SAS binary", 2: "Raw / unformatted"}

_RXROUTE_ENUM = {
    0: "Remote port 1",
    1: "Remote port 2",
    2: "Remote port 1 + echo to local other port",
    3: "Remote port 2 + echo to local other port",
}

_ENCRYPT_ENUM = {0: "Off", 1: "AES-128", 2: "AES-256"}

_ANTMODE_ENUM = {
    0: "Diversity (auto)",
    1: "A1 only",
    2: "A2 only",
    3: "TX on A1, RX on A2",
}

_PRI_ENUM = {1: "Low", 2: "Medium", 3: "High (RC input)"}

_BOOL_ENUM = {0: "Off", 1: "On"}

# §4.2.6 PIN#TYPE function table.
PIN_FUNC_GPI = 0
PIN_FUNC_RESET = 1
PIN_FUNC_MIRROR = 2
PIN_FUNC_GPO = 3
PIN_FUNC_GPO_WOR = 4
PIN_FUNC_GPO_WAND = 5
PIN_FUNC_STATLED = 6
PIN_FUNC_RXLED = 7
PIN_FUNC_PPM_IN = 9
PIN_FUNC_PPM_OUT = 10
PIN_FUNC_PPM_OUT_NOFS = 11
PIN_FUNC_SBUS_IN10 = 12
PIN_FUNC_SBUS_IN18 = 13
PIN_FUNC_SBUS_OUT = 14
PIN_FUNC_SBUS_OUT_NOFS = 15
PIN_FUNC_RTS = 16
PIN_FUNC_CTS = 17
PIN_FUNC_AUXTX = 18
PIN_FUNC_AUXRX = 19
PIN_FUNC_AUXRTS = 20
PIN_FUNC_AUXCTS = 21

PIN_FUNCTIONS: dict[int, str] = {
    0: "GPI (input)",
    1: "RESET (input)",
    2: "MIRROR (input → remote)",
    3: "GPO (output)",
    4: "GPO wired-OR",
    5: "GPO wired-AND",
    6: "STATLED (green LED)",
    7: "RXLED (red LED)",
    8: "Reserved",
    9: "PPM in",
    10: "PPM out (failsafe)",
    11: "PPM out (no failsafe)",
    12: "SBUS in, 10 ch",
    13: "SBUS in, 18 ch",
    14: "SBUS out (failsafe)",
    15: "SBUS out (no failsafe)",
    16: "RTS (port 1)",
    17: "CTS (port 1)",
    18: "AUX TX (port 2)",
    19: "AUX RX (port 2)",
    20: "AUX RTS (port 2)",
    21: "AUX CTS (port 2)",
    # Names from the v4.00 firmware's option list; the manual (§4.2.6) says
    # 22+ are "not intended for user functions".
    22: "485EN (internal)",
    23: "TDMSLOT (internal)",
    24: "RFTX (internal)",
    25: "RFRX (internal)",
    26: "RFLNAEN (internal)",
    27: "RFPAEN (internal)",
    28: "DBGSER (internal)",
    29: "ANT1EN (internal)",
    30: "ANT2EN (internal)",
}

RC_INPUT_FUNCS = frozenset({PIN_FUNC_PPM_IN, PIN_FUNC_SBUS_IN10, PIN_FUNC_SBUS_IN18})
RC_OUTPUT_FUNCS = frozenset({
    PIN_FUNC_PPM_OUT, PIN_FUNC_PPM_OUT_NOFS, PIN_FUNC_SBUS_OUT, PIN_FUNC_SBUS_OUT_NOFS,
})
RESERVED_PIN_FUNCS = frozenset({8})
INTERNAL_PIN_FUNCS = frozenset(range(22, 31))
# Functions that only make sense on a single pin at a time.
UNIQUE_PIN_FUNCS = frozenset({
    PIN_FUNC_RTS, PIN_FUNC_CTS, PIN_FUNC_AUXTX, PIN_FUNC_AUXRX,
    PIN_FUNC_AUXRTS, PIN_FUNC_AUXCTS,
}) | RC_INPUT_FUNCS | RC_OUTPUT_FUNCS

# The eight pin-function registers in PINMIRROR/PINDEFAULT index order.
# (name, sreg, x2 header pin, GPIO label)
PIN_REGISTERS: tuple[tuple[str, int, int, str], ...] = (
    ("PIN0TYPE", 35, 13, "GPIO0"),
    ("PIN1TYPE", 36, 15, "GPIO1"),
    ("PIN2TYPE", 37, 14, "GPIO2"),
    ("PIN3TYPE", 38, 12, "GPIO3"),
    ("PIN4TYPE", 39, 10, "GPIO4"),
    ("PIN5TYPE", 40, 8, "GPIO5"),
    ("PINRTSTYPE", 41, 3, "RTS"),
    ("PINCTSTYPE", 42, 11, "CTS"),
)

# Functions each pin accepts, decoded from the allowed-value masks v4.00
# firmware reports via ATI10:n (e.g. PIN1TYPE {0x80FF3D}).  These agree with
# the manual's §7.1 pinout table and add the internal functions.  A radio's
# own reported mask, when available, takes precedence (see ParamMeta).
def _bits(mask: int) -> frozenset[int]:
    return frozenset(i for i in range(31) if mask >> i & 1)


ALLOWED_PIN_FUNCS: dict[str, frozenset[int]] = {
    "PIN0TYPE": _bits(0x1000007F),
    "PIN1TYPE": _bits(0x0080FF3D),
    "PIN2TYPE": _bits(0x211000BD),
    "PIN3TYPE": _bits(0x4220007D),
    "PIN4TYPE": _bits(0x0408003D),
    "PIN5TYPE": _bits(0x0804003D),
    "PINRTSTYPE": _bits(0x0001003D),
    "PINCTSTYPE": _bits(0x0042003D),
}

# §4.1 S48 default.  §2.4 prints the same key with a leading 6 instead of
# 96 — both are treated as "the published default" by validation.
DEFAULT_ENCRYPTKEY: tuple[int, ...] = (
    96, 61, 235, 16, 21, 202, 113, 190, 43, 115, 174, 240, 133, 125, 119, 129,
    31, 53, 44, 7, 59, 97, 8, 215, 45, 152, 16, 163, 9, 20, 223, 244,
)
PUBLISHED_ENCRYPTKEYS: frozenset[tuple[int, ...]] = frozenset({
    DEFAULT_ENCRYPTKEY, (6,) + DEFAULT_ENCRYPTKEY[1:],
})

BROADCAST_DESTID = 63
MAX_NODEID = 62
MAX_SREG = 49

# Group headings used by the settings panel.
GROUP_LINK = "Link & RF"
GROUP_NETWORK = "Network & slots"
GROUP_ROUTING = "Routing & hops"
GROUP_ENCRYPTION = "Encryption"
GROUP_SERIAL1 = "Serial port 1"
GROUP_SERIAL2 = "Serial port 2"
GROUP_GPIO = "GPIO pins"
GROUP_RC = "RC passthrough"
GROUP_MISC = "Firmware"

_NET = "network"
_NODE = "node"


def _spec(
    name: str, label: str, tooltip: str, kind: str, lo, hi, default, *,
    enum=None, units=None, group="", scope=_NODE, length=None,
    read_only=False, notes="",
) -> ParamSpec:
    return ParamSpec(
        name=name, label=label, tooltip=tooltip, kind=kind,
        minimum=lo, maximum=hi, enum=dict(enum) if enum else None,
        default=default, units=units, variant_notes=notes,
        read_only=read_only, length=length, group=group, scope=scope,
    )


def _pin_spec(name: str, header_pin: int, gpio: str, default: int) -> ParamSpec:
    return _spec(
        name, f"{gpio} function (header pin {header_pin})",
        f"Function of {gpio}, x2 header pin {header_pin} (ux2 pin numbering "
        "differs). RC signals are only supported on GPIO1 (pin 15); UART 2 "
        "flow-control pins need the matching AUX functions.",
        "enum", 0, 30, default, enum=PIN_FUNCTIONS, group=GROUP_GPIO,
    )


# sreg → spec.  "scope" follows the manual's "Same for all nodes" column.
SPECS: dict[int, ParamSpec] = {
    0: _spec("FORMAT", "Parameter format",
             "Packet/parameter format identifier set by the firmware.",
             "int", None, None, None, group=GROUP_MISC, read_only=True, scope=_NET),
    1: _spec("BITRATE", "Air data rate",
             "RF bit rate in kbps. Not the usable throughput — slot overheads "
             "reduce it. Must match on every node.",
             "enum", None, None, 64, units="kbps", group=GROUP_LINK, scope=_NET,
             enum={v: f"{v} kbps" for v in BITRATES}),
    2: _spec("FREQMIN", "Minimum frequency",
             "Lower edge of the hopping band in kHz (900: 902000-928000, "
             "868: 865000-870000).",
             "int", 865000, 928000, 915000, units="kHz", group=GROUP_LINK, scope=_NET),
    3: _spec("FREQMAX", "Maximum frequency",
             "Upper edge of the hopping band in kHz.",
             "int", 865000, 928000, 921000, units="kHz", group=GROUP_LINK, scope=_NET),
    4: _spec("NUMCHANS", "Number of channels",
             "Hopping channels between FREQMIN and FREQMAX. Spacing "
             "(FREQMAX−FREQMIN)/(NUMCHANS+1) should be ≥500 kHz at 430/1000 "
             "kbps, ≥250 kHz otherwise.",
             "int", 1, 51, 23, group=GROUP_LINK, scope=_NET),
    5: _spec("XTRALONGRNG", "Extra-long range",
             "Lengthens inter-transmission gaps for >40 km links (balloons, "
             "air-to-air). Significantly reduces throughput.",
             "bool", None, None, 0, enum=_BOOL_ENUM, group=GROUP_LINK, scope=_NET),
    6: _spec("P2PAUTO", "Automatic master (P2P)",
             "1 = the first modem to boot becomes master (pairs only). "
             "0 = fixed master: NODEID 0 / SUBNETID 0 is master. Networks "
             "must use 0.",
             "bool", None, None, 1, enum=_BOOL_ENUM, group=GROUP_NETWORK, scope=_NET),
    7: _spec("TOTALUS", "Slot duration",
             "Length of one TDM slot in µs. Slot bytes ≈ TOTALUS×BITRATE/8000 "
             "must be ~300-1000; the firmware auto-adjusts on reboot otherwise.",
             "int", 5000, 220000, 39891, units="µs", group=GROUP_NETWORK, scope=_NET),
    8: _spec("SLOTS", "Number of slots",
             "TDM slots per network cycle — usually the node count, more if "
             "some nodes own several slots.",
             "int", 1, 64, 2, group=GROUP_NETWORK, scope=_NET),
    9: _spec("SLOTIDS", "Owned slots (bitmask)",
             "64-bit mask of slots this node may transmit in; bit 0 = slot 1. "
             "Must not overlap with any other node. The master owns bit 0.",
             "bitmask", 0, (1 << 64) - 1, 1, group=GROUP_NETWORK),
    10: _spec("NODEID", "Node ID",
              "This node's address, unique within its subnetwork. 0 is the "
              "master; 63/64 are reserved destination values.",
              "int", 0, MAX_NODEID, 0, group=GROUP_NETWORK),
    11: _spec("SUBNETID", "Subnetwork ID",
              "Subnetwork this node belongs to (0..SUBNETS−1). Data never "
              "crosses subnetworks. Subnet 0 holds the master.",
              "int", 0, 31, 0, group=GROUP_NETWORK, scope=_NODE),
    12: _spec("SUBNETS", "Number of subnetworks",
              "Subnetworks sharing this band under one master. Practical "
              "maximum is NUMCHANS−1.",
              "int", 1, 31, 1, group=GROUP_NETWORK, scope=_NET),
    13: _spec("BINDID", "Bind ID",
              "Separates co-located independent networks. Developmental — "
              "RFDesign advises leaving at 0 and using different bands.",
              "int", 0, 255, 0, group=GROUP_NETWORK, scope=_NET),
    14: _spec("DESTID", "Destination node",
              "Where this node's serial data goes: 0-62 = that NODEID, 63 = "
              "broadcast to the subnetwork. 64 is reserved.",
              "int", 0, 63, BROADCAST_DESTID, group=GROUP_ROUTING),
    15: _spec("HOPSMAX", "Maximum hops",
              "Network-wide maximum number of forwarding hops.",
              "int", 1, 7, 1, group=GROUP_ROUTING, scope=_NET),
    16: _spec("VALIDHOPSMIN", "Min accepted hops (debug)",
              "Debug: drop packets that took fewer hops. Leave at 0.",
              "int", 0, 7, 0, group=GROUP_ROUTING),
    17: _spec("VALIDHOPSMAX", "Max accepted hops (debug)",
              "Debug: drop packets that took more hops. Leave at 7.",
              "int", 0, 7, 7, group=GROUP_ROUTING),
    18: _spec("PRI", "Node priority",
              "3 = may take half of lower-priority slots (use on the RC input "
              "node); 2 = takes unused remainders; 1 = normal.",
              "enum", None, None, 1, enum=_PRI_ENUM, group=GROUP_ROUTING),
    19: _spec("SLOTTXMODE", "Slot TX mode (bitfield)",
              "bit0 = TX in own slots (always set), bit1 = TX in slots given "
              "up by others, bit2 = TX in sync slot (master). 0 = listen-only.",
              "int", 0, 7, 7, group=GROUP_ROUTING),
    20: _spec("ENCRYPTLVL", "Encryption level",
              "AES level; must match on every node.",
              "enum", None, None, 0, enum=_ENCRYPT_ENUM, group=GROUP_ENCRYPTION,
              scope=_NET),
    21: _spec("S1BAUD", "Port 1 baud",
              "Host UART baud for serial port 1. Changing it changes the baud "
              "rfdtool must reconnect at.",
              "enum", None, None, 57600, enum={b: str(b) for b in SERIAL_BAUDS},
              group=GROUP_SERIAL1),
    22: _spec("S1DATATYPE", "Port 1 data type",
              "0 = MAVLink only (non-MAVLink bytes are not passed!), 1 = SAS, "
              "2 = raw. Must match on every node.",
              "enum", None, None, 0, enum=_DATATYPE_ENUM, group=GROUP_SERIAL1,
              scope=_NET),
    23: _spec("S1KBPS", "Port 1 bandwidth limit",
              "Throttle in kbps. 0 DISABLES the port, 1000 = unlimited. Limits "
              "need flow control to be accurate.",
              "int", 0, 1000, 1000, units="kbps", group=GROUP_SERIAL1),
    24: _spec("S1RXROUTE", "Port 1 route",
              "Which port on the receiving node outputs data sent into port 1.",
              "enum", None, None, 0, enum=_RXROUTE_ENUM, group=GROUP_SERIAL1),
    25: _spec("S2BAUD", "Port 2 baud",
              "Baud for serial port 2 (AUX TX/RX pins).",
              "enum", None, None, 57600, enum={b: str(b) for b in SERIAL_BAUDS},
              group=GROUP_SERIAL2),
    26: _spec("S2DATATYPE", "Port 2 data type",
              "0 = MAVLink, 1 = SAS, 2 = raw. Must match on every node.",
              "enum", None, None, 0, enum=_DATATYPE_ENUM, group=GROUP_SERIAL2,
              scope=_NET),
    27: _spec("S2KBPS", "Port 2 bandwidth limit",
              "Throttle in kbps. 0 disables the port, 1000 = unlimited.",
              "int", 0, 1000, 1000, units="kbps", group=GROUP_SERIAL2),
    28: _spec("S2RXROUTE", "Port 2 route",
              "Which port on the receiving node outputs data sent into port 2.",
              "enum", None, None, 1, enum=_RXROUTE_ENUM, group=GROUP_SERIAL2),
    29: _spec("S2ATMODE", "Port 2 boots in AT mode",
              "Port 2 starts in AT command mode (no OK banner on boot).",
              "bool", None, None, 0, enum=_BOOL_ENUM, group=GROUP_SERIAL2),
    30: _spec("TXPOWER", "Transmit power",
              "RF output power in dBm (30 dBm = 1 W).",
              "int", 0, 30, 30, units="dBm", group=GROUP_LINK, scope=_NET),
    31: _spec("ANTMODE", "Antenna mode",
              "Antenna port use. Do not terminate an unused port with a load "
              "or short while in diversity mode.",
              "enum", None, None, 0, enum=_ANTMODE_ENUM, group=GROUP_LINK),
    32: _spec("RATE/FREQBAND", "Region rate/band option",
              "Selects the alternative RF parameter set on region-locked "
              "modems. No effect on unlocked modems.",
              "int", 0, 1, 0, group=GROUP_LINK, scope=_NET),
    33: _spec("RSVD1", "Reserved", "Reserved by the firmware.",
              "int", None, None, None, group=GROUP_MISC, read_only=True, scope=""),
    34: _spec("RSVD2", "Reserved", "Reserved by the firmware.",
              "int", None, None, None, group=GROUP_MISC, read_only=True, scope=""),
    **{
        sreg: _pin_spec(name, hdr, gpio, default)
        for (name, sreg, hdr, gpio), default in zip(
            PIN_REGISTERS, (1, 0, 20, 21, 19, 18, 16, 0),
        )
    },
    43: _spec("RCFRAMELOSS", "RC failsafe frame loss",
              "Missed RC frames (~20 ms each) before failsafe output.",
              "int", 5, 6666, 50, group=GROUP_RC),
    44: _spec("RCINMINMS", "RC min input interval",
              "RC input frames arriving sooner than this are dropped (limits "
              "air use from 7 ms SBUS sources).",
              "int", 5, 200, 12, units="ms", group=GROUP_RC),
    45: _spec("PINDEFAULT", "Pin default states",
              "Boot state (0/1) of each pin, PIN0..PIN5, RTS, CTS order.",
              "list", 0, 1, (1, 1, 0, 0, 0, 0, 0, 0), length=8, group=GROUP_GPIO),
    46: _spec("PINMIRROR", "Pin mirror map",
              "For each local MIRROR input (PIN0..7), the remote output pin "
              "that copies it. Outputs must not repeat.",
              "list", 0, 7, tuple(range(8)), length=8, group=GROUP_GPIO),
    47: _spec("RCDEFS", "RC failsafe values",
              "Failsafe output for 18 RC channels in µs (16 analog + 2 SBUS "
              "digital). Can be recorded live with AT&R.",
              "list", 900, 2100, (1500,) * 16 + (1000, 1000), length=18,
              group=GROUP_RC),
    48: _spec("ENCRYPTKEY", "AES key (32 bytes)",
              "32 comma-separated bytes; only the first 16 are used for AES-128. "
              "Must match on every node.",
              "list", 0, 255, DEFAULT_ENCRYPTKEY, length=32, group=GROUP_ENCRYPTION,
              scope=_NET),
    49: _spec("FWDTABLE", "Forwarding table",
              "10 source,destination pairs. Source 64 = unused, 63 = all "
              "broadcasts. Destination 64 = keep original, 63 = broadcast.",
              "list", 0, 64, (64,) * 20, length=20, group=GROUP_ROUTING),
}

# Alternate spellings seen in the manual (beta names, typos) mapped onto
# the canonical names above.  Matching is done on _norm()'d keys.
_ALIASES: dict[str, str] = {
    "XTRALONGRANGE": "XTRALONGRNG",
    "NETID": "SUBNETID",
    "NETWORKS": "SUBNETS",
    "ENCRYPTLV": "ENCRYPTLVL",
    "ENCRYPTLEVEL": "ENCRYPTLVL",
    "ENCRYPT": "ENCRYPTLVL",
    "PINMIRR": "PINMIRROR",
    "FREQBAND": "RATE/FREQBAND",
    "RATE": "RATE/FREQBAND",
}

NAME_TO_SREG: dict[str, int] = {spec.name: sreg for sreg, spec in SPECS.items()}
SREG_TO_NAME: dict[int, str] = {sreg: spec.name for sreg, spec in SPECS.items()}

# Names that are distinctive enough to identify MultiComm from an ATI5 dump
# (none of them exist in any SiK build).
_SIGNATURE_NAMES = frozenset({
    "BITRATE", "P2PAUTO", "TOTALUS", "SLOTS", "SLOTIDS", "NODEID",
    "SUBNETID", "DESTID", "HOPSMAX", "SLOTTXMODE", "S1BAUD", "FWDTABLE",
})


def _norm(name: str) -> str:
    """Upper-case, drop a trailing ``(pin)`` annotation and punctuation:
    ``"PIN0TYPE(13)"`` → ``"PIN0TYPE"``, ``"Rate/FreqBand"`` → ``"RATEFREQBAND"``."""
    base = re.sub(r"\(.*?\)", "", name)
    return re.sub(r"[^A-Z0-9]", "", base.upper())


_NORM_TO_NAME: dict[str, str] = {
    **{_norm(n): n for n in NAME_TO_SREG},
    **{_norm(alias): canon for alias, canon in _ALIASES.items()},
}


def canonical_name(name: str | None) -> str | None:
    """Map a firmware-reported name to this catalog's canonical name, or
    None if it isn't a known MultiComm parameter."""
    if not name:
        return None
    return _NORM_TO_NAME.get(_norm(name))


def spec_for(sreg: int, name: str | None = None) -> ParamSpec | None:
    """Catalog lookup for a MultiComm register.

    The firmware-reported ``name`` wins when it's recognisable (so a future
    firmware that renumbers registers still gets correct semantics); the
    manual's sreg numbering is the fallback when no name is available.
    """
    canon = canonical_name(name)
    if canon is not None:
        return SPECS[NAME_TO_SREG[canon]]
    if name:
        # A name we don't recognise — don't guess semantics from the sreg.
        return None
    return SPECS.get(sreg)


def canonicalize_names(s_names: dict[int, str]) -> dict[int, str]:
    """Rewrite firmware names to canonical catalog names where known, so
    presets, validation and diffs keyed by name work regardless of the
    exact spelling a firmware build prints (``PIN0TYPE(13)`` etc.)."""
    return {sreg: canonical_name(n) or n for sreg, n in s_names.items()}


def looks_like_multicomm(*, banner: str = "", names: object = ()) -> bool:
    """Heuristic family detection from an ``ATI`` banner and/or ATI5 names."""
    if banner and re.search(r"MULTI\s*-?\s*COMM", banner, re.IGNORECASE):
        return True
    found = {_NORM_TO_NAME.get(_norm(str(n))) for n in names}  # type: ignore[union-attr]
    return len(found & _SIGNATURE_NAMES) >= 3


def family_from(*, banner: str = "", names: object = ()) -> str | None:
    """Return ``FAMILY_MULTICOMM`` when detected, else None (caller decides)."""
    return FAMILY_MULTICOMM if looks_like_multicomm(banner=banner, names=names) else None


# --------------------------------------------------------------------- slot maths
def slot_bytes(totalus: int, bitrate: int) -> float:
    """§2.5.1: slot size in bytes ≈ TOTALUS × BITRATE / 8000."""
    return totalus * bitrate / 8000.0


SLOT_BYTES_MIN = 300
SLOT_BYTES_MAX = 1000


def channel_spacing_khz(freqmin: int, freqmax: int, numchans: int) -> float:
    """§4.2.5: spacing = (FREQMAX − FREQMIN) / (NUMCHANS + 1)."""
    return (freqmax - freqmin) / (numchans + 1)


def required_spacing_khz(bitrate: int) -> int:
    return 500 if bitrate >= 430 else 250


def slots_in(mask: int) -> list[int]:
    """Zero-based slot indices set in a SLOTIDS bitmask."""
    return [i for i in range(64) if mask >> i & 1]


def describe_slots(mask: int) -> str:
    """Human summary of a SLOTIDS mask, e.g. ``"slots 1, 4 (bits 0, 3)"``."""
    bits = slots_in(mask)
    if not bits:
        return "no slots"
    return (
        f"slot{'s' if len(bits) > 1 else ''} "
        f"{', '.join(str(b + 1) for b in bits)} "
        f"(bit{'s' if len(bits) > 1 else ''} {', '.join(map(str, bits))})"
    )


def is_master(by_name: dict[str, ParamValue]) -> bool:
    """§2.5.2.3: in fixed-master mode the master is NODEID 0 of SUBNETID 0."""
    return (
        by_name.get("P2PAUTO") == 0
        and by_name.get("NODEID") == 0
        and by_name.get("SUBNETID", 0) == 0
    )


# --------------------------------------------------------------------- RSSI report
_RSSI_LOC_NOISE = re.compile(
    r"Loc\s+RSSI\s+Noise\s+Av\s*:\s*(-?[\d.]+)", re.IGNORECASE,
)
_RSSI_LOC_NODE = re.compile(
    r"Loc\s+RSSI\s+Node\s*:\s*(\d+)\s+Av\s*:\s*(-?[\d.]+)", re.IGNORECASE,
)
_RSSI_REMOTE = re.compile(
    r"Remote\s+RSSI\s+Node\s*:\s*(\d+)\s+Av\s*:\s*(-?[\d.]+)"
    r"(?:\s+Noise\s*:\s*(-?[\d.]+))?",
    re.IGNORECASE,
)


def _dbm(text: str) -> float:
    # §8.2: values are reported as magnitudes of −dBm ("Av:91.00" = −91 dBm)
    # while the raw fields are sometimes printed already signed.
    return -abs(float(text))


def parse_rssi_report(text: str, *, node: int | None = None):
    """Parse ``AT&T=RSSI`` report lines (§8.2, figure 18) into an
    :class:`rfd.protocol.RssiReport` with values in dBm.

    ``node`` picks which remote NODEID populates ``remote_*``/``local_rssi``
    when several are reported; default is the lowest NODEID heard.  Later
    lines for the same node overwrite earlier ones (latest average wins).
    """
    from .protocol import NodeRssi, RssiReport

    local_noise: float | None = None
    local_by_node: dict[int, float] = {}
    remote_by_node: dict[int, tuple[float, float | None]] = {}
    for line in text.replace("\r", "\n").split("\n"):
        m = _RSSI_LOC_NOISE.search(line)
        if m:
            local_noise = _dbm(m.group(1))
            continue
        m = _RSSI_LOC_NODE.search(line)
        if m:
            local_by_node[int(m.group(1))] = _dbm(m.group(2))
            continue
        m = _RSSI_REMOTE.search(line)
        if m:
            remote_by_node[int(m.group(1))] = (
                _dbm(m.group(2)),
                _dbm(m.group(3)) if m.group(3) else None,
            )

    heard = sorted(set(local_by_node) | set(remote_by_node))
    nodes = tuple(
        NodeRssi(
            node=n,
            local_rssi=local_by_node.get(n),
            remote_rssi=remote_by_node.get(n, (None, None))[0],
            remote_noise=remote_by_node.get(n, (None, None))[1],
        )
        for n in heard
    )
    pick = node if node in heard else (heard[0] if heard else None)
    chosen = next((x for x in nodes if x.node == pick), None)
    return RssiReport(
        local_rssi=chosen.local_rssi if chosen else None,
        remote_rssi=chosen.remote_rssi if chosen else None,
        local_noise=local_noise,
        remote_noise=chosen.remote_noise if chosen else None,
        raw=text.strip(),
        units="dBm",
        linked=bool(heard) if (heard or local_noise is not None) else None,
        nodes=nodes,
    )

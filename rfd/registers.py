"""Parameter catalog for SiK-family radio firmware.

Two indexable views of every parameter rfdtool understands:

* :data:`CATALOG` — keyed by **parameter name** (e.g. ``"MAX_WINDOW"``).  The
  authoritative table of constraints, friendly labels, and tooltips.  This
  is what callers should consult when they know the parameter by name —
  e.g. when applying a preset, validating a value, or rendering a row in
  the settings tab built from the radio's own ``ATI5`` output.

* :data:`REGISTERS` — keyed by **canonical S-register number** (S0..S15 plus
  generic S16..S29 advanced rows).  Derived from CATALOG using the
  canonical SiK firmware mapping (see :data:`CANONICAL_SIK_NAMES`).  Kept
  for backwards compatibility with code that still hardcodes sreg numbers.

The split exists because RFDesign's SiK 3.x firmware reorders the registers
relative to the original ArduPilot/SiK enum: their S13/S14/S15 are
``RTSCTS``/``MAX_WINDOW``/``ENCRYPTION_LEVEL`` rather than canonical
``MANCHESTER``/``RTSCTS``/``MAX_WINDOW``.  Code that operates by name keeps
working across firmware variants; code that operates by sreg number is
correct only for the variant whose mapping it assumes.

For the canonical SiK semantics see ``Firmware/radio/parameters.h`` in
https://github.com/ArduPilot/SiK . For the RFDesign 3.x extensions, the
authoritative source is each radio's own ``ATI5`` response.
"""
from __future__ import annotations

from dataclasses import dataclass


# Firmware families rfdtool knows how to drive.  SiK covers canonical
# ArduPilot SiK plus RFDesign's SiK 2.x/3.x; MultiComm is RFDesign's
# mesh/multipoint firmware for V2 (x2/ux2) hardware, which uses an entirely
# different S-register map (see :mod:`rfd.multicomm`).
FAMILY_SIK = "sik"
FAMILY_MULTICOMM = "multicomm"

# A parameter value: scalar int for most registers, tuple of ints for the
# comma-separated array registers MultiComm exposes (ENCRYPTKEY, RCDEFS…).
ParamValue = int | tuple[int, ...]


@dataclass(frozen=True)
class ParamSpec:
    """A single parameter's semantic specification — no sreg number.

    Matches one entry in :data:`CATALOG`.  When combined with an sreg
    number (typically learned from ``ATI5``) you get a :class:`RegisterDef`
    via :func:`derive_def`.
    """
    name: str
    label: str
    tooltip: str
    kind: str                       # "int" | "enum" | "bool" | "bitmask" | "list"
    minimum: int | None             # for "list": per-element minimum
    maximum: int | None             # for "list": per-element maximum
    enum: dict[int, str] | None
    default: ParamValue | None
    units: str | None
    variant_notes: str = ""
    read_only: bool = False
    length: int | None = None       # "list" only: number of elements
    group: str = ""                 # UI section heading ("" = ungrouped)
    # "network" = must match on every node; "node" = per-node (often must
    # differ, e.g. NODEID); "" = unclassified.
    scope: str = ""


@dataclass(frozen=True)
class RegisterDef:
    """A parameter pinned to a specific sreg number on a specific firmware.

    Built dynamically by :func:`derive_def` from the catalog spec for that
    name plus the sreg number reported by the firmware.  Kept as a separate
    type for backwards compatibility with the original sreg-keyed API.
    """
    sreg: int
    name: str
    label: str
    tooltip: str
    kind: str
    minimum: int | None
    maximum: int | None
    enum: dict[int, str] | None
    default: ParamValue | None
    units: str | None
    variant_notes: str = ""
    read_only: bool = False
    length: int | None = None
    group: str = ""
    scope: str = ""


@dataclass(frozen=True)
class ParamMeta:
    """Constraints a MultiComm radio reports for one register.

    Parsed from the decorated form ``S1:BITRATE(N)[56..200]=200 {12,56,…,}``
    (``ATI10:n``, and ATI5 if a build prints it).  These are per-radio —
    an 868 MHz unit reports FREQMIN 865000..870000 and NUMCHANS 1..8 — so
    they override the manual's static catalog whenever available.

    ``type_code``: N numeric, B boolean, L list-of-labels, M 64-bit mask,
    A array.  ``mask`` (L only) is a bitfield of allowed values, 0 = any.
    ``length`` (A only) is the element count.
    """
    type_code: str = ""
    minimum: int | None = None
    maximum: int | None = None
    mask: int | None = None
    length: int | None = None
    options: tuple[str, ...] = ()

    def allowed_values(self) -> dict[int, str] | None:
        """value → label for enumerable params, honouring range and mask."""
        if not self.options:
            return None
        lo = self.minimum if self.minimum is not None else 0
        hi = self.maximum
        numeric = all(o.lstrip("-").isdigit() for o in self.options)
        out: dict[int, str] = {}
        if numeric and self.type_code == "N":
            # The option list is the value set itself (BITRATE, S1BAUD).  The
            # printed [min..max] is NOT a hard limit for these: a v4.00
            # RFD868x2 reports BITRATE [56..200] yet runs at 430, which is in
            # its option list.  So don't filter the list by the range.
            for o in self.options:
                out[int(o)] = o
            return out
        # Labels: index = value, except when there is exactly one label per
        # value in a range not starting at 0 (PRI 1..3 = Low/Med/High).
        offset = lo if hi is not None and len(self.options) == hi - lo + 1 else 0
        for i, label in enumerate(self.options):
            v = i + offset
            if v < lo or (hi is not None and v > hi):
                continue
            if self.mask and not self.mask >> v & 1:
                continue
            out[v] = label
        return out


def apply_meta(spec: "ParamSpec", meta: ParamMeta | None) -> "ParamSpec":
    """Overlay radio-reported constraints on a catalog spec.

    Keeps the catalog's label/tooltip/group/scope (and friendlier enum
    labels where the value is still allowed); takes kind, range, allowed
    values and array length from the radio.
    """
    if meta is None:
        return spec
    from dataclasses import replace

    kind, enum = spec.kind, spec.enum
    lo = meta.minimum if meta.minimum is not None else spec.minimum
    hi = meta.maximum if meta.maximum is not None else spec.maximum
    allowed = meta.allowed_values()
    if meta.type_code == "A":
        kind = "list"
    elif meta.type_code == "M":
        kind = "bitmask"
    elif allowed:
        kind = "bool" if meta.type_code == "B" and set(allowed) <= {0, 1} else "enum"
        nice = spec.enum or {}
        enum = {v: nice.get(v, label) for v, label in sorted(allowed.items())}
    elif meta.type_code == "N" and spec.kind not in ("int", "bitmask"):
        kind, enum = "int", None
    return replace(
        spec, kind=kind, minimum=lo, maximum=hi, enum=enum,
        length=meta.length if meta.length is not None else spec.length,
    )


# --------------------------------------------------------------------- values
def coerce_value(value: object) -> ParamValue:
    """Normalise a parameter value from any source (ATI5 text, JSON, editor)
    to :data:`ParamValue`.

    Lists/tuples and comma-separated strings (``"1,1,0,"`` — MultiComm's
    array syntax, trailing comma optional) become tuples of ints; anything
    else goes through ``int()``.
    """
    if isinstance(value, (list, tuple)):
        return tuple(int(v) for v in value)
    if isinstance(value, str):
        text = value.strip()
        if "," in text:
            return tuple(int(p) for p in text.split(",") if p.strip())
        return int(text)
    return int(value)  # type: ignore[arg-type]


def format_value(value: ParamValue) -> str:
    """Render a value in AT-command syntax.  Arrays get MultiComm's trailing
    comma (``ATS49=0,64,2,64,``), matching the firmware's own ATI5 output."""
    if isinstance(value, tuple):
        return "".join(f"{v}," for v in value)
    return str(int(value))


# --------------------------------------------------------------------- enums
_SERIAL_SPEED_ENUM: dict[int, str] = {
    1: "1200",  2: "2400",  4: "4800",  9: "9600",
    19: "19200", 38: "38400", 57: "57600", 115: "115200",
    230: "230400",
}


_AIR_SPEED_VALUES: tuple[int, ...] = (
    2, 4, 8, 16, 19, 24, 32, 48, 64, 96, 100, 125, 128, 192, 200, 224, 250,
)
_AIR_SPEED_ENUM: dict[int, str] = {v: f"{v} kbps" for v in _AIR_SPEED_VALUES}


_BOOL_ENUM: dict[int, str] = {0: "Off", 1: "On"}


_MAVLINK_ENUM: dict[int, str] = {
    0: "Raw data",
    1: "MAVLink framing",
    2: "Low-latency MAVLink",
}


_PIN_FUNCTION_ENUM: dict[int, str] = {
    0: "Disabled",
    1: "Digital input",
    2: "Digital output (low)",
    3: "Digital output (high)",
    4: "Analog input",
}


# --------------------------------------------------------------------- catalog
# Source of truth — every parameter name rfdtool understands plus its
# constraints. Adding a new firmware-specific name is one entry here.
CATALOG: dict[str, ParamSpec] = {
    # ---- canonical SiK + RFDesign common ----------------------------
    "FORMAT": ParamSpec(
        name="FORMAT",
        label="Parameter format",
        tooltip="Parameter format version reported by the firmware. "
                "Read-only; written by the radio itself.",
        kind="int", minimum=None, maximum=None, enum=None,
        default=None, units=None, read_only=True,
    ),
    "SERIAL_SPEED": ParamSpec(
        name="SERIAL_SPEED",
        label="Serial baud rate",
        tooltip="Baud rate of the host UART, expressed as the SiK shorthand "
                "(e.g. 57 = 57600 baud). Both ends of the link must agree "
                "with their respective hosts.",
        kind="enum", minimum=None, maximum=None,
        enum=dict(_SERIAL_SPEED_ENUM), default=57, units="kbps",
    ),
    "AIR_SPEED": ParamSpec(
        name="AIR_SPEED",
        label="Air data rate",
        tooltip="Over-the-air data rate in kbps. Lower rates trade "
                "throughput for range and link margin. Both ends must agree.",
        kind="enum", minimum=None, maximum=None,
        enum=dict(_AIR_SPEED_ENUM), default=64, units="kbps",
        variant_notes=(
            "RFD900/900+ supports the original SiK rate table up to 250 kbps. "
            "RFD900x/x2 expose a curated subset (12/56/64/100/125/200/250). "
            "The catalog encodes the superset so any reported value renders; "
            "per-variant legality is enforced by the validator."
        ),
    ),
    "NETID": ParamSpec(
        name="NETID",
        label="Network ID",
        tooltip="Network identifier. Both ends of a link must share the "
                "same NETID; radios on different NETIDs ignore each other.",
        kind="int", minimum=0, maximum=499, enum=None,
        default=25, units=None,
    ),
    "TXPOWER": ParamSpec(
        name="TXPOWER",
        label="Transmit power",
        tooltip="Transmit power in dBm. Higher values extend range at the "
                "cost of current draw and regulatory headroom.",
        kind="int", minimum=0, maximum=30, enum=None,
        default=20, units="dBm",
        variant_notes="Every variant (RFD900/a/u/+, RFD900x/ux/x2) is rated up "
                      "to 30 dBm (1 W); regional EIRP limits usually apply first.",
    ),
    "ECC": ParamSpec(
        name="ECC",
        label="Error correction (Golay)",
        tooltip="Enables Golay forward error correction. Halves usable "
                "throughput. Modern guidance is to leave this OFF — it "
                "reduces effective range on most radios.",
        kind="bool", minimum=None, maximum=None,
        enum=dict(_BOOL_ENUM), default=0, units=None,
    ),
    "MAVLINK": ParamSpec(
        name="MAVLINK",
        label="MAVLink framing",
        tooltip="Aligns radio packets with MAVLink message boundaries. "
                "Mode 2 additionally prioritises RC_OVERRIDE packets, "
                "useful for joystick control.",
        kind="enum", minimum=None, maximum=None,
        enum=dict(_MAVLINK_ENUM), default=1, units=None,
    ),
    "OPPRESEND": ParamSpec(
        name="OPPRESEND",
        label="Opportunistic resend",
        tooltip="Opportunistic packet resend. When enabled, the radio fills "
                "idle air time with retries of recent packets. Improves "
                "robustness at the cost of throughput predictability.",
        kind="bool", minimum=None, maximum=None,
        enum=dict(_BOOL_ENUM), default=0, units=None,
    ),
    "MIN_FREQ": ParamSpec(
        name="MIN_FREQ",
        label="Minimum frequency",
        tooltip="Lower bound of the frequency-hopping range, in kHz. Must "
                "lie within the band the radio's hardware supports.",
        kind="int", minimum=414000, maximum=976000, enum=None,
        default=None, units="kHz",
    ),
    "MAX_FREQ": ParamSpec(
        name="MAX_FREQ",
        label="Maximum frequency",
        tooltip="Upper bound of the frequency-hopping range, in kHz. Must "
                "be strictly greater than MIN_FREQ.",
        kind="int", minimum=414000, maximum=976000, enum=None,
        default=None, units="kHz",
    ),
    "NUM_CHANNELS": ParamSpec(
        name="NUM_CHANNELS",
        label="Number of hopping channels",
        tooltip="Number of frequency-hopping channels spread across "
                "MIN_FREQ..MAX_FREQ. More channels improve coexistence. "
                "RFDesign's newer firmwares accept values up to 255 (the "
                "uint8 ceiling); per-region regulatory minimums are checked "
                "separately by the validator.",
        kind="int", minimum=1, maximum=255, enum=None,
        default=20, units=None,
    ),
    "DUTY_CYCLE": ParamSpec(
        name="DUTY_CYCLE",
        label="Maximum duty cycle",
        tooltip="Cap on transmit duty cycle as a percentage. Most regions "
                "tolerate 100; EU sub-bands require 10 (or lower) plus LBT.",
        kind="int", minimum=10, maximum=100, enum=None,
        default=100, units="%",
    ),
    "LBT_RSSI": ParamSpec(
        name="LBT_RSSI",
        label="Listen-Before-Talk RSSI threshold",
        tooltip="Set to 0 to disable Listen-Before-Talk, or 25..220 for an "
                "RSSI threshold above which the radio backs off. EU "
                "regulators require LBT in the 433 MHz sub-bands; "
                "ETSI specifies ≥25.",
        kind="int", minimum=0, maximum=220, enum=None,
        default=0, units=None,
    ),
    "MANCHESTER": ParamSpec(
        name="MANCHESTER",
        label="Manchester encoding",
        tooltip="Manchester-encodes the air signal. Doubles air-bandwidth "
                "needs but improves robustness to DC drift. Present on "
                "canonical SiK firmwares; RFDesign 3.x removes it.",
        kind="bool", minimum=None, maximum=None,
        enum=dict(_BOOL_ENUM), default=0, units=None,
    ),
    "RTSCTS": ParamSpec(
        name="RTSCTS",
        label="RTS/CTS hardware flow control",
        tooltip="Hardware flow control on the host UART. Useful at high "
                "AIR_SPEED + SERIAL_SPEED combinations to prevent overrun.",
        kind="bool", minimum=None, maximum=None,
        enum=dict(_BOOL_ENUM), default=0, units=None,
    ),
    "MAX_WINDOW": ParamSpec(
        name="MAX_WINDOW",
        label="Maximum TDM window",
        tooltip="Maximum length of one TDM transmit window in milliseconds. "
                "Larger windows favour throughput; smaller windows favour "
                "latency (e.g. RC override). Newer firmwares may report 0 "
                "when the parameter is unused for that radio.",
        kind="int", minimum=0, maximum=131, enum=None,
        default=131, units="ms",
    ),

    # ---- RFDesign SiK 3.x extensions --------------------------------
    "ENCRYPTION_LEVEL": ParamSpec(
        name="ENCRYPTION_LEVEL",
        label="Encryption level",
        tooltip="On-air encryption strength. 0 = disabled. RFDesign 3.x "
                "exclusive; consult the firmware manual for valid levels.",
        kind="int", minimum=0, maximum=255, enum=None,
        default=0, units=None,
        variant_notes="RFDesign SiK 3.x only.",
    ),
    "ANT_MODE": ParamSpec(
        name="ANT_MODE",
        label="Antenna mode",
        tooltip="Antenna diversity / selection. RFDesign 3.x exclusive.",
        kind="int", minimum=0, maximum=255, enum=None,
        default=0, units=None,
    ),
    "AIR_FRAMELEN": ParamSpec(
        name="AIR_FRAMELEN",
        label="Air frame length",
        tooltip="Air-frame length parameter for the RFDesign 3.x scheduler. "
                "Affects effective throughput and packet alignment.",
        kind="int", minimum=0, maximum=255, enum=None,
        default=120, units=None,
    ),
    "RSSI_IN_DBM": ParamSpec(
        name="RSSI_IN_DBM",
        label="Report RSSI in dBm",
        tooltip="When on, RSSI fields in MAVLink RADIO_STATUS and ATI7 "
                "are reported as signed dBm rather than raw counts.",
        kind="bool", minimum=None, maximum=None,
        enum=dict(_BOOL_ENUM), default=0, units=None,
    ),
    "FSFRAMELOSS": ParamSpec(
        name="FSFRAMELOSS",
        label="Failsafe frame loss",
        tooltip="Number of consecutive frame losses before failsafe "
                "trigger. RFDesign 3.x exclusive.",
        kind="int", minimum=0, maximum=255, enum=None,
        default=50, units=None,
    ),
    "AUXSER_SPEED": ParamSpec(
        name="AUXSER_SPEED",
        label="Auxiliary UART speed",
        tooltip="Baud-rate shorthand for the second (auxiliary) UART on "
                "boards that expose one. Same encoding as SERIAL_SPEED.",
        kind="enum", minimum=None, maximum=None,
        enum=dict(_SERIAL_SPEED_ENUM), default=57, units="kbps",
    ),

    # ---- Pin/GPIO function selectors (RFDesign 3.x advanced) --------
    "GPI1_1R/CIN": ParamSpec(
        name="GPI1_1R/CIN",
        label="GPIO 1.1 — RC input",
        tooltip="Function selector for GPIO 1.1 acting as an RC input.",
        kind="int", minimum=0, maximum=255, enum=None, default=0, units=None,
    ),
    "GPO1_1R/COUT": ParamSpec(
        name="GPO1_1R/COUT",
        label="GPIO 1.1 — RC output",
        tooltip="Function selector for GPIO 1.1 acting as an RC output.",
        kind="int", minimum=0, maximum=255, enum=None, default=0, units=None,
    ),
    "GPO1_1SBUSIN": ParamSpec(
        name="GPO1_1SBUSIN",
        label="GPIO 1.1 — SBUS input",
        tooltip="Function selector for GPIO 1.1 acting as an SBUS input.",
        kind="int", minimum=0, maximum=255, enum=None, default=0, units=None,
    ),
    "GPO1_1SBUSOUT": ParamSpec(
        name="GPO1_1SBUSOUT",
        label="GPIO 1.1 — SBUS output",
        tooltip="Function selector for GPIO 1.1 acting as an SBUS output.",
        kind="int", minimum=0, maximum=255, enum=None, default=0, units=None,
    ),
    "GPO1_3STATLED": ParamSpec(
        name="GPO1_3STATLED",
        label="GPIO 1.3 — status LED",
        tooltip="GPIO 1.3 driving a link-status LED.",
        kind="int", minimum=0, maximum=255, enum=None, default=0, units=None,
    ),
    "GPO1_0TXEN485": ParamSpec(
        name="GPO1_0TXEN485",
        label="GPIO 1.0 — RS-485 TX enable",
        tooltip="GPIO 1.0 driving an RS-485 transceiver's TX-enable pin.",
        kind="int", minimum=0, maximum=255, enum=None, default=0, units=None,
    ),
    "RATE/FREQBAND": ParamSpec(
        name="RATE/FREQBAND",
        label="Rate / frequency band",
        tooltip="Combined rate and frequency-band selector. RFDesign 3.x "
                "internal; consult the firmware manual.",
        kind="int", minimum=0, maximum=255, enum=None, default=0, units=None,
    ),
    "GPI1_2AUXIN": ParamSpec(
        name="GPI1_2AUXIN",
        label="GPIO 1.2 — auxiliary input",
        tooltip="GPIO 1.2 acting as an auxiliary input.",
        kind="int", minimum=0, maximum=255, enum=None, default=0, units=None,
    ),
    "GPO1_3AUXOUT": ParamSpec(
        name="GPO1_3AUXOUT",
        label="GPIO 1.3 — auxiliary output",
        tooltip="GPIO 1.3 acting as an auxiliary output.",
        kind="int", minimum=0, maximum=255, enum=None, default=0, units=None,
    ),

    # ---- Pin / R-register names (RFDesign 3.x exposes named R-regs) -
    "PIN_FUNC": ParamSpec(
        name="PIN_FUNC",
        label="Pin function",
        tooltip="Selects the function of a GPIO pin. Pin mapping is "
                "available on RFD900x/ux/x2 only; older variants ignore "
                "these registers.",
        kind="enum", minimum=None, maximum=None,
        enum=dict(_PIN_FUNCTION_ENUM), default=0, units=None,
    ),
    "TARGET_RSSI_dBm": ParamSpec(
        name="TARGET_RSSI_dBm",
        label="Target RSSI (dBm)",
        tooltip="Target receive signal level in dBm. Used by adaptive "
                "TX power algorithms in RFDesign 3.x firmware.",
        kind="int", minimum=-127, maximum=127, enum=None,
        default=0, units="dBm",
    ),
    "HYSTERESIS_RSSI_dBm": ParamSpec(
        name="HYSTERESIS_RSSI_dBm",
        label="RSSI hysteresis (dBm)",
        tooltip="Hysteresis margin around the target RSSI before TX power "
                "adjusts.",
        kind="int", minimum=0, maximum=127, enum=None,
        default=5, units="dBm",
    ),
}


# --------------------------------------------------------------------- canonical mapping
# What sreg numbers the original ArduPilot/SiK firmware uses for each
# parameter.  Used:
#   * to populate :data:`REGISTERS` for backwards-compatible callers
#   * to convert v1/v2 JSON profiles (sreg-keyed) into name-keyed Profiles
#   * as the fallback sreg→name map in :func:`validation.validate_config`
#     when the caller doesn't supply the firmware's actual mapping
CANONICAL_SIK_NAMES: dict[int, str] = {
    0: "FORMAT",
    1: "SERIAL_SPEED",
    2: "AIR_SPEED",
    3: "NETID",
    4: "TXPOWER",
    5: "ECC",
    6: "MAVLINK",
    7: "OPPRESEND",
    8: "MIN_FREQ",
    9: "MAX_FREQ",
    10: "NUM_CHANNELS",
    11: "DUTY_CYCLE",
    12: "LBT_RSSI",
    13: "MANCHESTER",
    14: "RTSCTS",
    15: "MAX_WINDOW",
}


CANONICAL_PIN_NAMES: dict[int, str] = {n: "PIN_FUNC" for n in range(16)}


# Generic spec applied when a firmware reports a parameter name we don't
# have in CATALOG.  Lets the UI render the row with a sensible widget
# instead of a placeholder; the user can edit the value, and saves
# round-trip even though we don't know the parameter's semantics.
_GENERIC_PARAM = ParamSpec(
    name="ADV",
    label="(advanced)",
    tooltip="Advanced/firmware-specific register. Meaning depends on the "
            "exact firmware build — consult the radio's manual.",
    kind="int", minimum=0, maximum=65535, enum=None,
    default=None, units=None,
    variant_notes="Reported by firmware but not present in the rfdtool catalog.",
)


# --------------------------------------------------------------------- accessors
def get_spec(name: str) -> ParamSpec | None:
    """Return the catalog spec for `name`, or None if unknown."""
    return CATALOG.get(name)


def derive_def(
    sreg: int,
    name: str | None = None,
    *,
    family: str = FAMILY_SIK,
    meta: ParamMeta | None = None,
) -> RegisterDef:
    """Build a :class:`RegisterDef` for the given sreg and parameter name.

    ``family`` selects the catalog: MultiComm registers are looked up in
    :mod:`rfd.multicomm` (by sreg, cross-checked against the name) because
    several MultiComm names collide with SiK names that mean something
    else (e.g. ``TXPOWER`` defaults, ``RATE/FREQBAND`` range).

    If `name` is None or absent from the catalog, a generic int-spec is
    used so the UI still renders a row instead of a placeholder.
    """
    if family == FAMILY_MULTICOMM:
        from . import multicomm
        spec = multicomm.spec_for(sreg, name) or _GENERIC_PARAM
    else:
        spec = (CATALOG.get(name) if name else None) or _GENERIC_PARAM
    generic = spec is _GENERIC_PARAM
    spec = apply_meta(spec, meta)
    if generic:
        # Unknown name — synthesise a label that still identifies the row,
        # using sreg + name (if reported by firmware) so users can
        # correlate with manuals / forums.
        if name:
            display_label = f"S{sreg} {name} (advanced)"
        else:
            display_label = f"S{sreg} (advanced)"
    else:
        display_label = spec.label
    return RegisterDef(
        sreg=sreg,
        name=name or spec.name,
        label=display_label,
        tooltip=spec.tooltip,
        kind=spec.kind,
        minimum=spec.minimum,
        maximum=spec.maximum,
        enum=dict(spec.enum) if spec.enum is not None else None,
        default=spec.default,
        units=spec.units,
        variant_notes=spec.variant_notes,
        read_only=spec.read_only,
        length=spec.length,
        group=spec.group,
        scope=spec.scope,
    )


def validate_def(reg: RegisterDef | ParamSpec, value: ParamValue) -> tuple[bool, str]:
    """Validate `value` against a concrete row definition.

    Prefer this over the sreg-keyed :func:`validate` whenever the caller
    already holds the row's :class:`RegisterDef` — it's correct for every
    firmware layout, whereas :func:`validate` assumes canonical SiK sregs.
    """
    label = f"S{reg.sreg} {reg.name}" if isinstance(reg, RegisterDef) else reg.name
    return _validate_against_spec(reg, value, sreg_label=label)  # type: ignore[arg-type]


def validate_value(name: str, value: int) -> tuple[bool, str]:
    """Name-based validation. Unknown names accept any int value."""
    spec = CATALOG.get(name)
    if spec is None:
        return True, ""
    return _validate_against_spec(spec, value, sreg_label=name)


def _validate_against_spec(
    spec: ParamSpec, value: ParamValue, *, sreg_label: str = "",
) -> tuple[bool, str]:
    if spec.read_only:
        return False, f"{sreg_label or spec.name} is read-only"
    if spec.kind == "list":
        if not isinstance(value, tuple):
            return False, f"{sreg_label or spec.name}: expected a comma-separated list"
        if spec.length is not None and len(value) > spec.length:
            return False, (
                f"{sreg_label or spec.name}: {len(value)} entries "
                f"(maximum {spec.length})"
            )
        for v in value:
            if (spec.minimum is not None and v < spec.minimum) or (
                spec.maximum is not None and v > spec.maximum
            ):
                return False, (
                    f"{sreg_label or spec.name}: element {v} out of range "
                    f"({spec.minimum}..{spec.maximum})"
                )
        return True, ""
    if isinstance(value, tuple):
        return False, f"{sreg_label or spec.name}: expected a single number, not a list"
    if spec.kind in ("int", "bitmask"):
        if spec.minimum is not None and value < spec.minimum:
            return False, (
                f"{sreg_label or spec.name}: {value} out of range "
                f"({spec.minimum}..{spec.maximum})"
            )
        if spec.maximum is not None and value > spec.maximum:
            return False, (
                f"{sreg_label or spec.name}: {value} out of range "
                f"({spec.minimum}..{spec.maximum})"
            )
        return True, ""
    if spec.kind == "enum":
        if spec.enum and value in spec.enum:
            return True, ""
        return False, (
            f"{sreg_label or spec.name}: {value} is not a supported value"
        )
    if spec.kind == "bool":
        if value in (0, 1):
            return True, ""
        return False, (
            f"{sreg_label or spec.name}: {value} is not 0 or 1"
        )
    return False, f"{sreg_label or spec.name}: unknown kind {spec.kind!r}"


# --------------------------------------------------------------------- legacy sreg API
# Built once from CATALOG + CANONICAL_SIK_NAMES.  Adding generic
# advanced rows for S16..S29 mirrors what real RFDesign firmware returns.
REGISTERS: dict[int, RegisterDef] = {
    sreg: derive_def(sreg, name) for sreg, name in CANONICAL_SIK_NAMES.items()
}
for _sreg in range(16, 30):
    REGISTERS[_sreg] = derive_def(_sreg, None)


PIN_REGISTERS: dict[int, RegisterDef] = {}
for _pin in range(16):
    pin_name = CANONICAL_PIN_NAMES.get(_pin, "PIN_FUNC")
    pin_def = derive_def(_pin, pin_name)
    PIN_REGISTERS[_pin] = RegisterDef(
        sreg=pin_def.sreg,
        name=pin_def.name,
        label=f"Pin R{_pin} function",
        tooltip=pin_def.tooltip,
        kind=pin_def.kind,
        minimum=pin_def.minimum,
        maximum=pin_def.maximum,
        enum=pin_def.enum,
        default=pin_def.default,
        units=pin_def.units,
        variant_notes=pin_def.variant_notes,
        read_only=pin_def.read_only,
    )


def validate(sreg: int, value: int, *, pin: bool = False) -> tuple[bool, str]:
    """Legacy sreg-based validator. Looks up the canonical name for `sreg`
    in the appropriate map and delegates to :func:`validate_value`.

    Kept so existing callers (and tests) still work after the catalog refactor.
    """
    table = PIN_REGISTERS if pin else REGISTERS
    reg = table.get(sreg)
    if reg is None:
        prefix = "R" if pin else "S"
        return False, f"unknown register {prefix}{sreg}"
    return _validate_against_spec(
        CATALOG.get(reg.name) or _GENERIC_PARAM,
        value,
        sreg_label=f"S{sreg} {reg.name}" if not pin else f"R{sreg} {reg.name}",
    )


def all_registers() -> list[RegisterDef]:
    return [REGISTERS[s] for s in sorted(REGISTERS.keys())]


def all_pin_registers() -> list[RegisterDef]:
    return [PIN_REGISTERS[s] for s in sorted(PIN_REGISTERS.keys())]


def get_register(sreg: int, *, pin: bool = False) -> RegisterDef:
    table = PIN_REGISTERS if pin else REGISTERS
    return table[sreg]

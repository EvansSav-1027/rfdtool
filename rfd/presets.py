"""Configuration presets and JSON profile import/export.

A *Profile* is a serialisable bundle of parameter values plus optional
metadata.  The canonical storage is **name-keyed** (``params: dict[str, int]``)
because the same parameter can live at different sreg numbers on
different firmware variants — RFDesign's SiK 3.x reorders the canonical
S13/S14/S15.

For backwards compatibility, every Profile also exposes ``s_registers`` as
a sreg-keyed view via the canonical SiK mapping.  Constructors accept
either input form; ``__post_init__`` cross-fills the other.

JSON format versions:

* v1 — old format, ``s_registers`` only.
* v2 — adds ``applies_to`` / ``category`` / ``notes``, still sreg-keyed.
* v3 — adds the canonical name-keyed ``params`` field.  Later additions
  that older readers ignore: ``firmware`` (``"sik"`` / ``"multicomm"`` /
  ``""`` for any) and list-valued params (MultiComm arrays such as
  ``ENCRYPTKEY``), stored as JSON arrays.

The loader transparently upgrades v1 / v2 by mapping sregs through the
canonical SiK names.  Saving always writes v3 (with both ``params`` and
``s_registers`` for diagnostic clarity, but ``params`` is authoritative
on read-back).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path

from .registers import (
    CANONICAL_SIK_NAMES,
    FAMILY_MULTICOMM,
    FAMILY_SIK,
    ParamValue,
    coerce_value,
)

FORMAT_VERSION = 3

# Categories the UI uses to group presets.
CATEGORY_REGION = "region"
CATEGORY_USE_CASE = "use-case"
CATEGORY_MODEL = "model"
CATEGORY_MAXIMIZE = "maximize"
CATEGORY_MULTICOMM = "multicomm"
CATEGORY_USER = "user"

ALL_CATEGORIES = (
    CATEGORY_REGION,
    CATEGORY_USE_CASE,
    CATEGORY_MODEL,
    CATEGORY_MAXIMIZE,
    CATEGORY_MULTICOMM,
    CATEGORY_USER,
)


# Reverse lookup used everywhere for legacy → canonical conversion.
_CANONICAL_NAME_TO_SREG: dict[str, int] = {
    n: s for s, n in CANONICAL_SIK_NAMES.items()
}


@dataclass
class Profile:
    name: str
    description: str = ""
    # Canonical name-keyed parameter map.  Preferred for new code.  Values
    # are ints, or tuples for MultiComm array parameters.
    params: dict[str, ParamValue] = field(default_factory=dict)
    # Legacy sreg-keyed view of the same data.  Cross-filled in
    # __post_init__ from `params` (or vice versa) using the canonical SiK
    # mapping.  Existing test/UI code still uses this; new code should
    # prefer `params`.
    s_registers: dict[int, int] = field(default_factory=dict)
    pin_registers: dict[int, int] = field(default_factory=dict)
    # v2 metadata — all default-empty so v1 files load cleanly.
    applies_to: list[str] = field(default_factory=list)
    category: str = CATEGORY_USER
    notes: str = ""
    # Firmware family the params are written for ("" = unspecified, the
    # default for profiles saved before MultiComm support).
    firmware: str = ""

    def __post_init__(self) -> None:
        self.params = {k: coerce_value(v) for k, v in self.params.items()}
        # Cross-fill the legacy and canonical views so both stay in sync
        # for the canonical SiK parameter set (S0..S15).  Names absent from
        # the canonical mapping (RFDesign 3.x extras) live only in
        # ``params`` and don't appear in ``s_registers``.  MultiComm names
        # mean something else at those sregs, so never cross-fill them.
        if self.firmware == FAMILY_MULTICOMM:
            return
        if self.params and not self.s_registers:
            self.s_registers = {
                _CANONICAL_NAME_TO_SREG[n]: int(v)  # type: ignore[arg-type]
                for n, v in self.params.items()
                if n in _CANONICAL_NAME_TO_SREG and not isinstance(v, tuple)
            }
        elif self.s_registers and not self.params:
            self.params = {
                CANONICAL_SIK_NAMES[s]: int(v)
                for s, v in self.s_registers.items()
                if s in CANONICAL_SIK_NAMES
            }

    def to_dict(self) -> dict:
        return {
            "format_version": FORMAT_VERSION,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "applies_to": list(self.applies_to),
            "notes": self.notes,
            "firmware": self.firmware,
            "params": {
                k: list(v) if isinstance(v, tuple) else int(v)
                for k, v in self.params.items()
            },
            "s_registers": {str(k): int(v) for k, v in self.s_registers.items()},
            "pin_registers": {str(k): int(v) for k, v in self.pin_registers.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        version = int(d.get("format_version", 1))
        if version not in (1, 2, 3):
            raise ValueError(f"unsupported profile format_version {version}")
        s_registers = {int(k): int(v) for k, v in (d.get("s_registers") or {}).items()}
        pin = {int(k): int(v) for k, v in (d.get("pin_registers") or {}).items()}
        # v3+: prefer name-keyed `params`.  Otherwise let __post_init__
        # synthesise it from s_registers via the canonical mapping.
        params: dict[str, ParamValue] = {}
        if version >= 3:
            params = {str(k): coerce_value(v) for k, v in (d.get("params") or {}).items()}
        return cls(
            name=str(d.get("name") or ""),
            description=str(d.get("description") or ""),
            params=params,
            s_registers=s_registers if not params else {},
            pin_registers=pin,
            applies_to=list(d.get("applies_to") or []),
            category=str(d.get("category") or CATEGORY_USER),
            notes=str(d.get("notes") or ""),
            firmware=str(d.get("firmware") or ""),
        )

    def matches_firmware(self, family: str) -> bool:
        """True if this profile is meant for `family` (or isn't tagged)."""
        return not self.firmware or not family or self.firmware == family

    def matches_board(self, board_name: str) -> bool:
        if not self.applies_to:
            return True
        if not board_name:
            return True
        lowered = board_name.lower()
        return any(b.lower() in lowered for b in self.applies_to)

    def to_sregs_for(self, name_to_sreg: dict[str, int]) -> dict[int, ParamValue]:
        """Translate ``params`` to a sreg-keyed dict using the radio's
        actual ``name → sreg`` map (typically learned from ATI5).

        Names absent from ``name_to_sreg`` (parameters the connected radio
        doesn't have) are silently skipped — the apply path then leaves
        the corresponding row alone instead of writing nonsense to
        whichever sreg the canonical mapping would have picked.
        """
        out: dict[int, ParamValue] = {}
        for n, v in self.params.items():
            sreg = name_to_sreg.get(n)
            if sreg is None:
                continue
            out[sreg] = coerce_value(v)
        return out


# --------------------------------------------------------------------- JSON I/O
def save_profile(path: str | PathLike[str], profile: Profile) -> None:
    Path(path).write_text(json.dumps(profile.to_dict(), indent=2), encoding="utf-8")


def load_profile(path: str | PathLike[str]) -> Profile:
    return Profile.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# --------------------------------------------------------------------- user storage
def user_profile_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    p = Path(base) / "rfdtool" / "profiles"
    p.mkdir(parents=True, exist_ok=True)
    return p


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    s = _SLUG_RE.sub("-", name.lower()).strip("-")
    return s or "preset"


def list_user_profiles() -> list[Profile]:
    out: list[Profile] = []
    for p in sorted(user_profile_dir().glob("*.json")):
        try:
            out.append(load_profile(p))
        except Exception:
            continue
    return out


def save_user_profile(profile: Profile, *, overwrite: bool = True) -> Path:
    profile.category = profile.category or CATEGORY_USER
    target = user_profile_dir() / f"{slugify(profile.name)}.json"
    if target.exists() and not overwrite:
        raise FileExistsError(target)
    save_profile(target, profile)
    return target


def delete_user_profile(profile: Profile) -> bool:
    target = user_profile_dir() / f"{slugify(profile.name)}.json"
    if target.exists():
        target.unlink()
        return True
    return False


# --------------------------------------------------------------------- live capture
def profile_from_ati5(
    name: str,
    ati5: object,
    *,
    radio_info: dict | None = None,
    description: str = "",
    notes: str = "",
) -> Profile:
    """Build a Profile from a fresh ATI5 read.

    Captures the firmware's reported parameter NAMES (so RFDesign 3.x extras
    like ENCRYPTION_LEVEL and AIR_FRAMELEN survive the round-trip) plus a
    Markdown block of identity metadata in ``notes`` for traceability of
    where the config came from.

    Skips read-only parameters (e.g. FORMAT) — those are firmware-set and
    aren't worth re-applying to the destination radio.

    ``ati5`` is duck-typed so this function doesn't have to import the
    Qt-touching protocol module; in practice it's :class:`Ati5Result` from
    ``rfd.protocol``.
    """
    s_params: dict[int, ParamValue] = dict(getattr(ati5, "s_params", {}) or {})
    s_names: dict[int, str] = dict(getattr(ati5, "s_names", {}) or {})
    pin_params: dict[int, int] = dict(getattr(ati5, "pin_params", {}) or {})
    family = str(getattr(ati5, "family", "") or FAMILY_SIK)

    # Build name-keyed params using the firmware's own names; skip names we
    # know to be read-only.
    from .registers import derive_def
    params: dict[str, ParamValue] = {}
    for sreg, value in s_params.items():
        pname = s_names.get(sreg)
        if not pname:
            continue
        if derive_def(sreg, pname, family=family).read_only:
            continue
        params[pname] = coerce_value(value)

    # Optional metadata header inside notes for traceability.
    meta_lines: list[str] = []
    if radio_info:
        meta_lines.append("Captured from:")
        for key in ("banner", "board_name", "board_id", "freq_id",
                    "bootloader_version"):
            v = radio_info.get(key)
            if v is None or v == "":
                continue
            meta_lines.append(f"  - {key}: {v}")
    full_notes = "\n".join(meta_lines + ([notes] if notes else [])).strip()

    # applies_to defaults to the source board name when known, so applying
    # this profile to a different SKU triggers ApplyPresetDialog's mismatch
    # warning.
    applies_to: list[str] = []
    if radio_info and radio_info.get("board_name"):
        bn = str(radio_info["board_name"])
        # Skip the unknown placeholder names emitted by protocol.board_name()
        if bn and not bn.lower().startswith("unknown"):
            applies_to.append(bn)

    return Profile(
        name=name,
        description=description,
        params=params,
        pin_registers=pin_params,
        applies_to=applies_to,
        category=CATEGORY_USER,
        notes=full_notes,
        firmware=family,
    )


# --------------------------------------------------------------------- built-ins
# Defined name-keyed so they apply correctly across firmware variants.
# The settings tab translates names to sregs using the radio's reported
# ATI5 mapping; names absent on a given firmware are silently skipped.

# ------------- A. Region & frequency
REGION_PRESETS: list[Profile] = [
    Profile(
        name="US / Canada — 902-928 MHz",
        description="FCC 15.247 / IC RSS-210 — full ISM band, 50 channels.",
        category=CATEGORY_REGION,
        params={
            "MIN_FREQ": 902000, "MAX_FREQ": 928000,
            "NUM_CHANNELS": 50, "DUTY_CYCLE": 100, "LBT_RSSI": 0,
        },
        notes="FCC 15.247 (US) / IC RSS-210 Annex 8.1 (Canada). License-free.",
    ),
    Profile(
        name="Australia — 915-928 MHz",
        description="ACMA LIPD-2015 — narrower band, ≤1 W EIRP.",
        category=CATEGORY_REGION,
        params={
            "MIN_FREQ": 915001, "MAX_FREQ": 928000,
            "NUM_CHANNELS": 20, "DUTY_CYCLE": 100, "LBT_RSSI": 0,
        },
        notes="ACMA LIPD-2015. ≤1 W EIRP including antenna gain.",
    ),
    Profile(
        name="New Zealand — 921-928 MHz",
        description="NZ General User Radio Licence — 7 MHz sub-band.",
        category=CATEGORY_REGION,
        params={
            "MIN_FREQ": 921000, "MAX_FREQ": 928000,
            "NUM_CHANNELS": 20, "DUTY_CYCLE": 100, "LBT_RSSI": 0,
        },
        notes="NZ Notice 2007 Schedule 1 (General User Radio Licence).",
    ),
    Profile(
        name="Brazil — 915-928 MHz",
        description="ANATEL Resolução 506/2008 — at least 26 channels.",
        category=CATEGORY_REGION,
        params={
            "MIN_FREQ": 915000, "MAX_FREQ": 928000,
            "NUM_CHANNELS": 26, "DUTY_CYCLE": 100, "LBT_RSSI": 0,
        },
        notes="ANATEL Resolução 506/2008.",
    ),
    Profile(
        name="EU / UK — 433 MHz ISM",
        description="ETSI EN 300 220 — 10% duty cycle, LBT required.",
        category=CATEGORY_REGION,
        applies_to=["RFD900u"],
        params={
            "MIN_FREQ": 433050, "MAX_FREQ": 434790,
            "NUM_CHANNELS": 10, "DUTY_CYCLE": 10, "LBT_RSSI": 25,
        },
        notes=(
            "ETSI EN 300 220 V3.1.1 / OFCOM IR2030/1/10. Mandatory 10% duty "
            "cycle and LBT (≥25). ≤7 dBm conducted."
        ),
    ),
    Profile(
        name="Australia — 433 MHz",
        description="ACMA LIPD — ≤25 mW EIRP (≈14 dBm).",
        category=CATEGORY_REGION,
        applies_to=["RFD900u"],
        params={
            "MIN_FREQ": 433051, "MAX_FREQ": 434790,
            "NUM_CHANNELS": 10, "DUTY_CYCLE": 100, "LBT_RSSI": 0,
        },
        notes="ACMA LIPD class licence. ≤25 mW EIRP.",
    ),
]


# ------------- B. Use case (no MIN_FREQ/MAX_FREQ/NUM_CHANNELS/DUTY_CYCLE/LBT_RSSI/SERIAL_SPEED/NETID)
USE_CASE_PRESETS: list[Profile] = [
    Profile(
        name="MAVLink default (SiK factory)",
        description="Stock SiK factory: 64 kbps air, 20 dBm, MAVLink framing on, ECC off.",
        category=CATEGORY_USE_CASE,
        params={
            "AIR_SPEED": 64, "TXPOWER": 20, "ECC": 0, "MAVLINK": 1,
            "OPPRESEND": 0, "MANCHESTER": 0, "RTSCTS": 0, "MAX_WINDOW": 131,
        },
        notes=(
            "Matches SiK firmware defaults. ECC is intentionally OFF — current "
            "ArduPilot guidance: 'Using error correction is no longer "
            "recommended due to the range reduction.'"
        ),
    ),
    Profile(
        name="MAVLink + RC joystick (low-latency)",
        description="Low-latency MAVLink: prioritises RC_OVERRIDE, small TDM window.",
        category=CATEGORY_USE_CASE,
        params={
            "AIR_SPEED": 64, "TXPOWER": 20, "ECC": 0, "MAVLINK": 2,
            "OPPRESEND": 0, "MANCHESTER": 0, "RTSCTS": 0, "MAX_WINDOW": 33,
        },
        notes=(
            "MAVLINK=2 enables low-latency MAVLink with RC_OVERRIDE priority. "
            "MAX_WINDOW=33 ms minimises TDM window for joystick responsiveness."
        ),
    ),
    Profile(
        name="MAVLink high throughput",
        description="128 kbps air with MAVLink framing + RTSCTS for high telemetry rates.",
        category=CATEGORY_USE_CASE,
        params={
            "AIR_SPEED": 128, "TXPOWER": 20, "ECC": 0, "MAVLINK": 2,
            "OPPRESEND": 0, "MANCHESTER": 0, "RTSCTS": 1, "MAX_WINDOW": 131,
        },
        notes="RTSCTS recommended at high air rates to prevent UART overrun.",
    ),
    Profile(
        name="Long range — 8 kbps (20 dBm)",
        description="8 kbps air rate, 20 dBm, MAVLink framing — extends range without external power.",
        category=CATEGORY_USE_CASE,
        params={
            "AIR_SPEED": 8, "TXPOWER": 20, "ECC": 0, "MAVLINK": 1,
            "OPPRESEND": 0, "MANCHESTER": 0, "RTSCTS": 0, "MAX_WINDOW": 131,
        },
        notes=(
            "Lower air rate trades throughput for receiver sensitivity (≈9 dB "
            "improvement vs. 64 kbps). ECC kept off per ArduPilot guidance."
        ),
    ),
    Profile(
        name="Long range — 8 kbps + 1 W TX",
        description="8 kbps air rate at 30 dBm (1 W) — maximum range.",
        category=CATEGORY_USE_CASE,
        applies_to=["RFD900x", "RFD900ux", "RFD900x2"],
        params={
            "AIR_SPEED": 8, "TXPOWER": 30, "ECC": 0, "MAVLINK": 1,
            "OPPRESEND": 0, "MANCHESTER": 0, "RTSCTS": 0, "MAX_WINDOW": 131,
        },
        notes=(
            "Requires external ≥2 A power supply; autopilot telemetry ports "
            "typically can't deliver this. Combine with a region preset."
        ),
    ),
    Profile(
        name="Max throughput — 250 kbps + RTSCTS",
        description="Highest practical air rate, MAVLink framing OFF.",
        category=CATEGORY_USE_CASE,
        params={
            "AIR_SPEED": 250, "TXPOWER": 20, "ECC": 0, "MAVLINK": 0,
            "OPPRESEND": 0, "MANCHESTER": 0, "RTSCTS": 1, "MAX_WINDOW": 131,
        },
        notes="Best for raw serial bridging or non-MAVLink data with strong link margin.",
    ),
    Profile(
        name="Raw serial bridge",
        description="64 kbps, no MAVLink framing, no ECC — generic UART tunnel.",
        category=CATEGORY_USE_CASE,
        params={
            "AIR_SPEED": 64, "TXPOWER": 20, "ECC": 0, "MAVLINK": 0,
            "OPPRESEND": 0, "MANCHESTER": 0, "RTSCTS": 0, "MAX_WINDOW": 131,
        },
        notes="Use when bridging arbitrary serial data (modbus, NMEA, …) instead of MAVLink.",
    ),
    Profile(
        name="Robust (Manchester encoding)",
        description="Manchester-encoded for noisy RF environments; 8 kbps.",
        category=CATEGORY_USE_CASE,
        params={
            "AIR_SPEED": 8, "TXPOWER": 20, "ECC": 0, "MAVLINK": 1,
            "OPPRESEND": 0, "MANCHESTER": 1, "RTSCTS": 0, "MAX_WINDOW": 131,
        },
        notes=(
            "Manchester encoding doubles air-bandwidth requirements but improves "
            "robustness against DC drift and certain interference patterns. "
            "MANCHESTER is canonical-SiK only — RFDesign 3.x firmware drops "
            "the parameter; this preset's value is silently skipped on those radios."
        ),
    ),
]


# ------------- C. Model factory defaults
_FACTORY_PARAMS = {
    "SERIAL_SPEED": 57, "AIR_SPEED": 64, "NETID": 25, "TXPOWER": 20,
    "ECC": 0, "MAVLINK": 1, "OPPRESEND": 0, "DUTY_CYCLE": 100,
    "MANCHESTER": 0, "RTSCTS": 0, "MAX_WINDOW": 131,
}


MODEL_PRESETS: list[Profile] = [
    Profile(
        name="RFD900 / RFD900+ / RFD900u — factory defaults",
        description="SiK firmware factory defaults for the 8051 family.",
        category=CATEGORY_MODEL,
        applies_to=["RFD900", "RFD900a", "RFD900p", "RFD900u"],
        params=dict(_FACTORY_PARAMS),
        notes="Apply a Region preset afterwards to set MIN_FREQ/MAX_FREQ/NUM_CHANNELS.",
    ),
    Profile(
        name="RFD900x / RFD900ux — factory defaults",
        description="SiK firmware factory defaults for the STM32 family (SiK 2.x).",
        category=CATEGORY_MODEL,
        applies_to=["RFD900x", "RFD900ux"],
        params=dict(_FACTORY_PARAMS),
        notes="Apply a Region preset afterwards to set MIN_FREQ/MAX_FREQ/NUM_CHANNELS.",
    ),
    Profile(
        name="RFD900x2 — factory defaults",
        description="SiK 3.x factory defaults for the RFD900x2.",
        category=CATEGORY_MODEL,
        applies_to=["RFD900x2"],
        params=dict(_FACTORY_PARAMS),
        notes=(
            "Apply a Region preset afterwards to set MIN_FREQ/MAX_FREQ/NUM_CHANNELS. "
            "Newer firmware adds ENCRYPTION_LEVEL and GPIO regs which are left "
            "untouched by this preset."
        ),
    ),
]


# ------------- D. "Maximize" all-in-one combos
MAXIMIZE_PRESETS: list[Profile] = [
    Profile(
        name="Maximize MAVLink reliability — US (RFD900x/x2)",
        description="64 kbps + low-latency MAVLink, US 902-928 MHz, 20 dBm.",
        category=CATEGORY_MAXIMIZE,
        applies_to=["RFD900x", "RFD900ux", "RFD900x2"],
        params={
            "AIR_SPEED": 64, "TXPOWER": 20, "ECC": 0, "MAVLINK": 2,
            "OPPRESEND": 0,
            "MIN_FREQ": 902000, "MAX_FREQ": 928000,
            "NUM_CHANNELS": 50, "DUTY_CYCLE": 100, "LBT_RSSI": 0,
            "MANCHESTER": 0, "RTSCTS": 0, "MAX_WINDOW": 131,
        },
        notes="Best autopilot default in the US/Canada band. FCC 15.247 compliant.",
    ),
    Profile(
        name="Maximize range — US, 1 W (RFD900x/x2)",
        description="8 kbps + 30 dBm, US 902-928 MHz — needs external power.",
        category=CATEGORY_MAXIMIZE,
        applies_to=["RFD900x", "RFD900ux", "RFD900x2"],
        params={
            "AIR_SPEED": 8, "TXPOWER": 30, "ECC": 0, "MAVLINK": 1,
            "OPPRESEND": 0,
            "MIN_FREQ": 902000, "MAX_FREQ": 928000,
            "NUM_CHANNELS": 50, "DUTY_CYCLE": 100, "LBT_RSSI": 0,
            "MANCHESTER": 0, "RTSCTS": 0, "MAX_WINDOW": 131,
        },
        notes=(
            "30 dBm peak draws ≈2 A — supply external power, do not run from "
            "an autopilot telemetry port. FCC 15.247 compliant."
        ),
    ),
    Profile(
        name="Maximize throughput — US (RFD900x/x2)",
        description="250 kbps + RTSCTS, US 902-928 MHz, 20 dBm.",
        category=CATEGORY_MAXIMIZE,
        applies_to=["RFD900x", "RFD900ux", "RFD900x2"],
        params={
            "AIR_SPEED": 250, "TXPOWER": 20, "ECC": 0, "MAVLINK": 0,
            "OPPRESEND": 0,
            "MIN_FREQ": 902000, "MAX_FREQ": 928000,
            "NUM_CHANNELS": 50, "DUTY_CYCLE": 100, "LBT_RSSI": 0,
            "MANCHESTER": 0, "RTSCTS": 1, "MAX_WINDOW": 131,
        },
        notes="MAVLink framing off; pair with raw serial workflows.",
    ),
    Profile(
        name="Maximize MAVLink reliability — AU (RFD900x/x2)",
        description="64 kbps + low-latency MAVLink, AU 915-928 MHz, 20 dBm.",
        category=CATEGORY_MAXIMIZE,
        applies_to=["RFD900x", "RFD900ux", "RFD900x2"],
        params={
            "AIR_SPEED": 64, "TXPOWER": 20, "ECC": 0, "MAVLINK": 2,
            "OPPRESEND": 0,
            "MIN_FREQ": 915001, "MAX_FREQ": 928000,
            "NUM_CHANNELS": 20, "DUTY_CYCLE": 100, "LBT_RSSI": 0,
            "MANCHESTER": 0, "RTSCTS": 0, "MAX_WINDOW": 131,
        },
        notes="ACMA LIPD-2015 compliant.",
    ),
    Profile(
        name="Maximize range — AU, 1 W (RFD900x/x2)",
        description="8 kbps + 30 dBm, AU 915-928 MHz — needs external power.",
        category=CATEGORY_MAXIMIZE,
        applies_to=["RFD900x", "RFD900ux", "RFD900x2"],
        params={
            "AIR_SPEED": 8, "TXPOWER": 30, "ECC": 0, "MAVLINK": 1,
            "OPPRESEND": 0,
            "MIN_FREQ": 915001, "MAX_FREQ": 928000,
            "NUM_CHANNELS": 20, "DUTY_CYCLE": 100, "LBT_RSSI": 0,
            "MANCHESTER": 0, "RTSCTS": 0, "MAX_WINDOW": 131,
        },
        notes="≤1 W EIRP per ACMA LIPD-2015. Supply external power.",
    ),
]


# Everything above is written for SiK parameter names.
for _p in REGION_PRESETS + USE_CASE_PRESETS + MODEL_PRESETS + MAXIMIZE_PRESETS:
    _p.firmware = FAMILY_SIK


# ------------- E. RFD MultiComm (x2/ux2 hardware)
# Transcribed from §5 "Example Modem Configurations" of the RFD MultiComm
# Firmware User Manual v1.0.  Settings the manual doesn't mention are left
# alone, as the manual assumes defaults for them.
_MC_NOTE = (
    "From the RFD MultiComm manual §5. Reboot every node after saving. "
    "TOTALUS may be auto-adjusted by the firmware — check it matches on all nodes."
)


def _mc(name: str, description: str, params: dict, notes: str = _MC_NOTE) -> Profile:
    return Profile(
        name=f"MultiComm · {name}",
        description=description,
        category=CATEGORY_MULTICOMM,
        params=params,
        notes=notes,
        firmware=FAMILY_MULTICOMM,
    )


_MC_FACTORY = {
    # §4.1 defaults, minus the region-dependent band settings (left alone so
    # region-locked modems aren't pushed out of their band) and read-only regs.
    "BITRATE": 64, "XTRALONGRNG": 0, "P2PAUTO": 1, "TOTALUS": 39891,
    "SLOTS": 2, "SLOTIDS": 1, "NODEID": 0, "SUBNETID": 0, "SUBNETS": 1,
    "BINDID": 0, "DESTID": 63, "HOPSMAX": 1, "VALIDHOPSMIN": 0,
    "VALIDHOPSMAX": 7, "PRI": 1, "SLOTTXMODE": 7, "ENCRYPTLVL": 0,
    "S1BAUD": 57600, "S1DATATYPE": 0, "S1KBPS": 1000, "S1RXROUTE": 0,
    "S2BAUD": 57600, "S2DATATYPE": 0, "S2KBPS": 1000, "S2RXROUTE": 1,
    "S2ATMODE": 0, "TXPOWER": 30, "ANTMODE": 0,
    "PIN0TYPE": 1, "PIN1TYPE": 0, "PIN2TYPE": 20, "PIN3TYPE": 21,
    "PIN4TYPE": 19, "PIN5TYPE": 18, "PINRTSTYPE": 16, "PINCTSTYPE": 0,
    "RCFRAMELOSS": 50, "RCINMINMS": 12,
    "PINDEFAULT": (1, 1, 0, 0, 0, 0, 0, 0),
    "PINMIRROR": tuple(range(8)),
    "RCDEFS": (1500,) * 16 + (1000, 1000),
    "FWDTABLE": (64,) * 20,
}

# India 865–868 MHz at 1 W EIRP with the antennas shipped with the RFD868x.
# EIRP = TXPOWER + antenna gain (cable/connector loss ignored, so real EIRP
# is a little lower).  TXPOWER is whole dBm, so round *down*.
_IN_BAND = {
    "FREQMIN": 865000, "FREQMAX": 868000,
    # 8 is the RFD868x's NUMCHANS maximum: 3000/(8+1) = 333 kHz spacing,
    # above the 250 kHz needed at ≤200 kbps; centres 865.33–867.67 MHz.
    "NUMCHANS": 8,
    "BITRATE": 64,
}
_IN_NOTE = (
    "EIRP = TXPOWER + antenna gain, ignoring cable loss (real EIRP slightly "
    "lower). Apply to every node: band, channels and air rate must match. "
    "REGULATORY: India's licence exemption for 865–868 MHz (GSR 853(E), "
    "10 Dec 2021) allows non-specific SRD/telemetry 25 mW e.r.p. at 1% duty "
    "cycle, or 500 mW e.r.p. with APC at ≤2.5% duty for tracking/data "
    "acquisition. It superseded the 2005 865–867 MHz rule (1 W / 4 W e.r.p.), "
    "which now covers only equipment type-approved under it. 1 W EIRP needs "
    "a licence or such a type approval — MultiComm has no duty-cycle limiter."
)

# §5.3.1 four-node single network at 125 kbps.
_MC_NET = {"BITRATE": 125, "P2PAUTO": 0, "TOTALUS": 25000, "SLOTS": 10}

MULTICOMM_PRESETS: list[Profile] = [
    _mc("India 865–868 MHz, 1 W EIRP — stock 2 dBi monopole",
        "RFD868x with its stock 868 MHz monopole: 28 dBm + 2 dBi = 30 dBm (1.0 W) EIRP.",
        {**_IN_BAND, "TXPOWER": 28},
        notes=_IN_NOTE),
    _mc("India 865–868 MHz, 1 W EIRP — stock dipole (2.15 dBi)",
        "RFD868x with a half-wave dipole: 27 dBm + 2.15 dBi = 29.15 dBm (0.82 W) "
        "EIRP; 28 dBm would be 1.04 W.",
        {**_IN_BAND, "TXPOWER": 27},
        notes=_IN_NOTE),
    _mc("Factory defaults",
        "Manual §4.1 defaults for every setting except the frequency band.",
        _MC_FACTORY,
        notes="Leaves FREQMIN/FREQMAX/NUMCHANS/RATE-FREQBAND alone. For a full "
              "reset including the band, use Factory Reset (AT&F)."),
    _mc("P2P — automatic master (both radios)",
        "Apply the same settings to both radios; whichever boots first becomes master.",
        {"P2PAUTO": 1, "TOTALUS": 40000, "SLOTS": 2, "SLOTIDS": 2, "NODEID": 1},
        notes="Manual §5.1.1.1. Power the radios up a few seconds apart for the "
              "fastest link. " + _MC_NOTE),
    _mc("P2P — fixed master: master (radio A)",
        "Radio A is always master (NODEID 0, slot 1). Links faster than auto mode.",
        {"P2PAUTO": 0, "TOTALUS": 40000, "SLOTS": 2, "SLOTIDS": 1, "NODEID": 0},
        notes="Manual §5.1.1.2. Pair with the 'fixed master: node' preset on "
              "radio B. " + _MC_NOTE),
    _mc("P2P — fixed master: node (radio B)",
        "Radio B is NODEID 1 in slot 2.",
        {"P2PAUTO": 0, "TOTALUS": 40000, "SLOTS": 2, "SLOTIDS": 2, "NODEID": 1},
        notes="Manual §5.1.1.2. " + _MC_NOTE),
    _mc("RC passthrough — PPM input (ground side)",
        "GPIO1 (pin 15) reads PPM; priority 3 for minimum control latency.",
        {"TOTALUS": 40000, "PRI": 3, "PIN1TYPE": 9},
        notes="Manual §5.1.2 with pin function codes from the §4.2.6 table "
              "(the §5.1.2 example's codes are off by one). 3.3 V logic on "
              "pin 15; ground on pin 16. " + _MC_NOTE),
    _mc("RC passthrough — SBUS input, 18 ch (ground side)",
        "GPIO1 (pin 15) reads 16+2 channel SBUS; priority 3.",
        {"TOTALUS": 40000, "PRI": 3, "PIN1TYPE": 13},
        notes="Manual §5.1.2 / §4.2.6. Use code 12 instead for 10-channel SBUS "
              "to save bandwidth. " + _MC_NOTE),
    _mc("RC passthrough — PPM output with failsafe (vehicle side)",
        "GPIO1 (pin 15) outputs PPM; RCDEFS is the failsafe.",
        {"TOTALUS": 40000, "PIN1TYPE": 10},
        notes="Manual §5.1.2. Record a failsafe with AT&R while linked, or set "
              "RCDEFS. " + _MC_NOTE),
    _mc("RC passthrough — SBUS output with failsafe (vehicle side)",
        "GPIO1 (pin 15) outputs SBUS; RCDEFS is the failsafe.",
        {"TOTALUS": 40000, "PIN1TYPE": 14},
        notes="Manual §5.1.2. " + _MC_NOTE),
    _mc("Listen-only node",
        "Receives broadcasts but never transmits (needs a fixed-master network).",
        # Owns no slots, so it can't collide with the master's slot 1.
        {"SLOTTXMODE": 0, "P2PAUTO": 0, "SLOTIDS": 0},
        notes="Manual §2.6 / §5.2. Also give it a NODEID that is unused in "
              "its subnetwork and not 0 (NODEID 0 / SUBNETID 0 is the master). "
              + _MC_NOTE),
    _mc("Raw serial data (non-MAVLink) on port 1",
        "Pass any byte stream. The default (MAVLink) drops non-MAVLink bytes.",
        {"S1DATATYPE": 2},
        notes="Manual §4.2.4. Data type must match on every node."),
    _mc("Hardware flow control on port 1",
        "Enable RTS (pin 3) and CTS (pin 11) for serial port 1.",
        {"PINRTSTYPE": 16, "PINCTSTYPE": 17},
        notes="Manual §4.2.4: flow control is needed for bandwidth limits to "
              "work and recommended when serial rate exceeds air throughput."),
    _mc("Network (4 nodes, 125 kbps) — master",
        "Master of the manual's 4-node single-network example.",
        {**_MC_NET, "SLOTIDS": 1, "NODEID": 0},
        notes="Manual §5.3.1. Set DESTID per node: 63 to broadcast or a NODEID "
              "for unicast. " + _MC_NOTE),
    *[
        _mc(f"Network (4 nodes, 125 kbps) — node {n}",
            f"Node {n} of the manual's 4-node single-network example.",
            {**_MC_NET, "SLOTIDS": 1 << n, "NODEID": n},
            notes="Manual §5.3.1. " + _MC_NOTE)
        for n in range(1, 5)
    ],
]


BUILT_IN_PRESETS: list[Profile] = (
    REGION_PRESETS + USE_CASE_PRESETS + MODEL_PRESETS + MAXIMIZE_PRESETS
    + MULTICOMM_PRESETS
)


def find_preset(name: str) -> Profile | None:
    for p in BUILT_IN_PRESETS:
        if p.name == name:
            return p
    return None


def presets_by_category(category: str) -> list[Profile]:
    return [p for p in BUILT_IN_PRESETS if p.category == category]


def presets_for_board(board_name: str, *, category: str | None = None) -> list[Profile]:
    pool = (
        BUILT_IN_PRESETS if category is None
        else presets_by_category(category)
    )
    return [p for p in pool if p.matches_board(board_name)]

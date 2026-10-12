"""Regional regulatory data for RFD900-series radios.

Single source of truth used by both the preset library (to build
region-specific presets) and the validator (to flag illegal/suboptimal
configurations).  Pulled verbatim from the ArduPilot wiki's "Telemetry
Radio Regional Regulations" page plus the RFDesign datasheets and the
ETSI / FCC source documents cited inline.

This module is pure data + small lookup helpers; no I/O, no Qt.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Region:
    code: str                  # short identifier ("US", "AU", "EU433", …)
    name: str                  # human-readable label for menus/dialogs
    citation: str              # regulatory citation
    min_freq: int              # kHz, inclusive
    max_freq: int              # kHz, inclusive
    min_channels: int | None   # regulatory minimum NUM_CHANNELS, or None
    max_tx_dbm: int | None     # regulatory max TX power in dBm, or None
    duty_cycle: int | None     # required DUTY_CYCLE %, or None for "100"
    lbt_rssi_min: int | None   # required minimum LBT_RSSI threshold
    notes: str = ""

    def covers(self, s8: int, s9: int) -> bool:
        """True if (S8, S9) falls entirely within this region's allowed range."""
        return s8 >= self.min_freq and s9 <= self.max_freq


# Order matters for tie-breaking in detect_region(): more-restrictive ranges
# should come *after* more-permissive ones with the same band, since we sort
# candidates by range width and pick the narrowest match.
REGIONS: list[Region] = [
    Region(
        code="US",
        name="United States / Canada (902–928 MHz)",
        citation="FCC 15.247 / IC RSS-210 Annex 8.1",
        min_freq=902000, max_freq=928000,
        min_channels=50, max_tx_dbm=30, duty_cycle=100, lbt_rssi_min=None,
    ),
    Region(
        code="AU",
        name="Australia (915–928 MHz)",
        citation="ACMA LIPD-2015",
        min_freq=915001, max_freq=928000,
        min_channels=20, max_tx_dbm=30, duty_cycle=100, lbt_rssi_min=None,
        notes="≤1 W EIRP including antenna gain.",
    ),
    Region(
        code="NZ",
        name="New Zealand (921–928 MHz)",
        citation="NZ General User Radio Licence — Notice 2007 Schedule 1",
        min_freq=921000, max_freq=928000,
        min_channels=20, max_tx_dbm=None, duty_cycle=100, lbt_rssi_min=None,
    ),
    Region(
        code="BR",
        name="Brazil (915–928 MHz)",
        citation="ANATEL Resolução 506/2008",
        min_freq=915000, max_freq=928000,
        min_channels=26, max_tx_dbm=None, duty_cycle=100, lbt_rssi_min=None,
    ),
    Region(
        code="AR",
        name="Argentina (902–928 MHz)",
        citation="ENACOM Resolución SC 302/98",
        min_freq=902000, max_freq=928000,
        min_channels=50, max_tx_dbm=None, duty_cycle=100, lbt_rssi_min=None,
    ),
    # 868 MHz band (RFD868x): informational entries — the regulatory limits
    # are per sub-band / device class and don't map onto single SiK values,
    # so no channel/power/duty numbers are enforced for them.
    Region(
        code="IN865",
        name="India (865–868 MHz)",
        citation="GSR 853(E), 10 Dec 2021",
        min_freq=865000, max_freq=868000,
        min_channels=None, max_tx_dbm=None, duty_cycle=None, lbt_rssi_min=None,
        notes="Licence-exempt SRD limits: 25 mW e.r.p. at 1% duty (telemetry/"
              "data), or 500 mW e.r.p. with APC at ≤2.5% duty (tracking/data "
              "acquisition). Higher power needs a licence or type approval.",
    ),
    Region(
        code="EU868",
        name="EU / UK 863–870 MHz SRD",
        citation="ETSI EN 300 220 / ERC REC 70-03 Annex 1",
        min_freq=863000, max_freq=870000,
        min_channels=None, max_tx_dbm=None, duty_cycle=None, lbt_rssi_min=None,
        notes="Power and duty-cycle limits depend on the sub-band (typically "
              "25 mW e.r.p. at 0.1–1% duty; 500 mW at 869.4–869.65 MHz, 10%).",
    ),
    Region(
        code="EU433",
        name="EU / UK 433 MHz ISM",
        citation="ETSI EN 300 220 V3.1.1 / OFCOM IR2030/1/10",
        min_freq=433050, max_freq=434790,
        min_channels=None, max_tx_dbm=7, duty_cycle=10, lbt_rssi_min=25,
        notes="10% duty cycle, LBT required, ≤7 dBm conducted.",
    ),
    Region(
        code="AU433",
        name="Australia 433 MHz",
        citation="ACMA LIPD",
        min_freq=433051, max_freq=434790,
        min_channels=None, max_tx_dbm=14, duty_cycle=100, lbt_rssi_min=None,
        notes="≤25 mW EIRP (≈14 dBm).",
    ),
    Region(
        code="ZA433",
        name="South Africa 433 MHz",
        citation="ICASA",
        min_freq=433050, max_freq=434790,
        min_channels=None, max_tx_dbm=10, duty_cycle=None, lbt_rssi_min=None,
        notes="≤10 mW EIRP.",
    ),
]


def detect_region(s8: int, s9: int) -> Region | None:
    """Match a (MIN_FREQ, MAX_FREQ) configuration to a regulatory region.

    A user's range may fit inside several regions (e.g. 915–928 MHz fits both
    the US 902–928 envelope and the AU 915–928 envelope).  We return the
    *narrowest* matching region, since that's the one the user is most
    likely targeting.  Returns None if no region covers the range.
    """
    if s8 <= 0 or s9 <= 0 or s8 >= s9:
        return None
    candidates = [r for r in REGIONS if r.covers(s8, s9)]
    if not candidates:
        return None
    candidates.sort(key=lambda r: r.max_freq - r.min_freq)
    return candidates[0]


def find_region(code: str) -> Region | None:
    for r in REGIONS:
        if r.code == code:
            return r
    return None


# --------------------------------------------------------------------- model TX limits
# Per-board TX power ceilings (dBm) — based on RFDesign datasheets and
# community-validated reports.  Boards above these limits silently clip.
MODEL_MAX_TXPOWER: dict[str, int] = {
    # RFDesign rates every RFD900 variant at up to 1 W (+30 dBm).
    "RFD900":   30,
    "RFD900a":  30,
    "RFD900u":  30,
    "RFD900p":  30,
    "RFD900x":  30,
    "RFD900ux": 30,
    "RFD900x2": 30,
}


def model_max_txpower(board_name: str) -> int | None:
    """Return the TX-power ceiling for `board_name`, or None if unknown.

    Matches by case-insensitive substring so banner-style strings like
    'RFD900X2-US' still resolve.
    """
    if not board_name:
        return None
    lowered = board_name.lower()
    # Prefer the most-specific (longest) name that's a substring of board_name
    matches = [(name, cap) for name, cap in MODEL_MAX_TXPOWER.items()
               if name.lower() in lowered]
    if not matches:
        return None
    matches.sort(key=lambda kv: -len(kv[0]))
    return matches[0][1]


# --------------------------------------------------------------------- model families
@dataclass(frozen=True)
class ModelFamily:
    code: str               # "8051", "stm32_v1", "stm32_v2"
    name: str
    boards: tuple[str, ...]


MODEL_FAMILIES: list[ModelFamily] = [
    ModelFamily("8051", "RFD900 / RFD900+ / RFD900u (8051)",
                ("RFD900", "RFD900a", "RFD900p", "RFD900u")),
    ModelFamily("stm32_v1", "RFD900x / RFD900ux (STM32, SiK 2.x)",
                ("RFD900x", "RFD900ux")),
    ModelFamily("stm32_v2", "RFD900x2 (STM32, SiK 3.x)",
                ("RFD900x2",)),
]


# --------------------------------------------------------------------- firmware lockdowns
# Some RFDesign firmware variants ship with frequency-hopping and air-rate
# parameters factory-locked, so the radio stays inside its regulatory
# certification.  Any AT write to these registers returns "ERROR".  We use
# this map to pre-flight which proposed changes the radio will reject so the
# user sees the warning *before* clicking Save.
#
# Structure: list of (banner-substring, frozenset of locked S-regs).  First
# match wins.  Substring match is case-insensitive.
FIRMWARE_LOCKDOWNS: list[tuple[str, frozenset[int]]] = [
    # RFD900x2-US — confirmed via direct serial probing on hardware:
    # S2=64, S8=902000, S9=915000, S10=51, S15=0 are all rejected on change.
    ("RFD900X2-US", frozenset({2, 8, 9, 10, 15})),
    # RFD900x-US — same firmware family, same expected lockdown set.
    ("RFD900X-US", frozenset({2, 8, 9, 10, 15})),
    # RFD900x2-EU / RFD900x-EU — RFDesign's EU SKUs lock to ETSI sub-bands;
    # the parameter set is the same as the US lockdown.  (Marked as expected;
    # update once verified on hardware.)
    ("RFD900X2-EU", frozenset({2, 8, 9, 10, 15})),
    ("RFD900X-EU",  frozenset({2, 8, 9, 10, 15})),
]


def firmware_lockdown(banner: str) -> frozenset[int]:
    """Return the set of S-registers known to be firmware-locked on the
    radio whose ATI banner contains the matched substring.

    Empty banner or unknown variant → empty set (no rule applied).
    """
    if not banner:
        return frozenset()
    upper = banner.upper()
    for substr, locked in FIRMWARE_LOCKDOWNS:
        if substr.upper() in upper:
            return locked
    return frozenset()


def lockdown_label(banner: str) -> str:
    """Return a short human label for the locked SKU in `banner`, or ''."""
    if not banner:
        return ""
    upper = banner.upper()
    for substr, _ in FIRMWARE_LOCKDOWNS:
        if substr.upper() in upper:
            return substr
    return ""


def model_family(board_name: str) -> ModelFamily | None:
    if not board_name:
        return None
    lowered = board_name.lower()
    # Prefer the family whose most-specific board name matches — sort all
    # candidate (family, board) pairs by board-name length descending so
    # "RFD900x2" beats "RFD900".
    matches: list[tuple[int, ModelFamily]] = []
    for fam in MODEL_FAMILIES:
        for b in fam.boards:
            if b.lower() in lowered:
                matches.append((len(b), fam))
    if not matches:
        return None
    matches.sort(key=lambda kv: -kv[0])
    return matches[0][1]

"""Higher-level configuration validation rules for SiK-family radios.

Operates in **parameter-name space** rather than sreg-number space, so the
same rule fires correctly regardless of which sreg a particular firmware
puts a parameter at.  RFDesign's SiK 3.x reorders S13/S14/S15 relative to
canonical SiK; both layouts go through the same rules here as long as the
caller passes the firmware's ``sreg → name`` map (typically built from the
firmware's own ``ATI5`` response).

The validator composes 19 small rule functions (``_rule_1`` ..
``_rule_19``).  Each returns a list of issues; ``validate_config`` simply
chains them.  ``ValidationIssue.sregs`` is still populated for the
existing UI; ``param_names`` carries the canonical name of each register
the issue concerns.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import multicomm as mc
from . import registers, regions
from .registers import FAMILY_MULTICOMM, ParamValue
from .regions import (
    Region,
    detect_region,
    model_family,
    model_max_txpower,
)


# Air rates that look like "telemetry" links (R14 / R15 / R16 / R17 use
# this same notion of "telemetry-class" rates, ie. anything plausible for
# a MAVLink ground-station link rather than a fast point-to-point bridge).
_TELEMETRY_AIR_RATES: frozenset[int] = frozenset({64, 96, 128, 192, 200, 250})


@dataclass(frozen=True)
class ValidationIssue:
    severity: str                          # "error" | "warning" | "info"
    sregs: tuple[int, ...]                 # firmware-specific sreg numbers
    title: str                             # short label
    detail: str                            # 1-2 sentences of explanation
    fix_hint: str = ""                     # human-readable suggestion
    suggested_value: ParamValue | None = None  # for click-to-fix
    citation: str = ""                     # regulatory citation if applicable
    param_names: tuple[str, ...] = ()      # canonical parameter names


@dataclass(frozen=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...]
    detected_region: Region | None
    detected_board: str

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def infos(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "info"]

    @property
    def overall(self) -> str:
        """Worst severity present: 'errors' | 'warnings' | 'info' | 'ok'."""
        if self.errors:
            return "errors"
        if self.warnings:
            return "warnings"
        if self.infos:
            return "info"
        return "ok"

    @property
    def issues_by_sreg(self) -> dict[int, list[ValidationIssue]]:
        """Index issues by the registers they concern.

        An issue spanning multiple S-registers (e.g. R2 covering S8 and S9)
        appears under each key so the UI can tint every affected row.
        """
        out: dict[int, list[ValidationIssue]] = {}
        for issue in self.issues:
            for sreg in issue.sregs:
                out.setdefault(sreg, []).append(issue)
        return out

    @property
    def issues_by_name(self) -> dict[str, list[ValidationIssue]]:
        """Index issues by canonical parameter name (where known)."""
        out: dict[str, list[ValidationIssue]] = {}
        for issue in self.issues:
            for name in issue.param_names:
                out.setdefault(name, []).append(issue)
        return out


# --------------------------------------------------------------------- helpers
def _sreg_label(sreg: int | None, name: str) -> str:
    return f"S{sreg} {name}" if sreg is not None else name


def _make_issue(
    *,
    severity: str,
    names: tuple[str, ...],
    name_to_sreg: dict[str, int],
    title: str,
    detail: str,
    fix_hint: str = "",
    suggested_value: ParamValue | None = None,
    citation: str = "",
) -> ValidationIssue:
    sregs = tuple(name_to_sreg[n] for n in names if n in name_to_sreg)
    return ValidationIssue(
        severity=severity,
        sregs=sregs,
        title=title,
        detail=detail,
        fix_hint=fix_hint,
        suggested_value=suggested_value,
        citation=citation,
        param_names=names,
    )


# --------------------------------------------------------------------- rules
# Each rule operates on a name-keyed view of the configuration.  Issues
# carry both the canonical name(s) and the sreg numbers (translated via
# `name_to_sreg`) so the UI can tint the right rows regardless of which
# sreg the firmware happens to use for that parameter.

def _rule_1(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
) -> list[ValidationIssue]:
    """R1 — per-parameter range / enum / bool validation.

    Read-only parameters (e.g. FORMAT) are skipped — the user isn't
    writing them, and the per-name validator returns "is read-only" for
    any value of theirs.  Names absent from the catalog are accepted (the
    firmware says they exist; we don't have an opinion on their range).
    """
    issues: list[ValidationIssue] = []
    for name in sorted(by_name):
        spec = registers.get_spec(name)
        if spec is not None and spec.read_only:
            continue
        value = by_name[name]
        ok, reason = registers.validate_value(name, value)
        if not ok:
            sreg = name_to_sreg.get(name)
            issues.append(ValidationIssue(
                severity="error",
                sregs=(sreg,) if sreg is not None else (),
                title=f"{_sreg_label(sreg, name)}: invalid value {value}",
                detail=reason,
                param_names=(name,),
            ))
    return issues


def _rule_2(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
) -> list[ValidationIssue]:
    """R2 — frequency ordering: MIN_FREQ must be strictly less than MAX_FREQ."""
    if "MIN_FREQ" not in by_name or "MAX_FREQ" not in by_name:
        return []
    if by_name["MIN_FREQ"] >= by_name["MAX_FREQ"]:
        return [_make_issue(
            severity="error",
            names=("MIN_FREQ", "MAX_FREQ"),
            name_to_sreg=name_to_sreg,
            title="MIN_FREQ ≥ MAX_FREQ",
            detail="MIN_FREQ must be strictly less than MAX_FREQ; the "
                   "radio will refuse the configuration.",
        )]
    return []


def _rule_3(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
) -> list[ValidationIssue]:
    """R3 — channel spacing must be at least 50 kHz."""
    if not all(n in by_name for n in ("MIN_FREQ", "MAX_FREQ", "NUM_CHANNELS")):
        return []
    s8 = by_name["MIN_FREQ"]
    s9 = by_name["MAX_FREQ"]
    s10 = by_name["NUM_CHANNELS"]
    if s10 <= 0 or s8 >= s9:
        return []
    spacing = (s9 - s8) / s10
    if spacing < 50:
        return [_make_issue(
            severity="warning",
            names=("MIN_FREQ", "MAX_FREQ", "NUM_CHANNELS"),
            name_to_sreg=name_to_sreg,
            title="Channels too narrow",
            detail=(
                f"NUM_CHANNELS={s10} across {s9 - s8} kHz gives "
                f"{spacing:.0f} kHz/channel; SiK needs at least 50 kHz spacing."
            ),
        )]
    return []


def _rule_4(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
    board_name: str,
) -> list[ValidationIssue]:
    """R4 — TX power must not exceed the board's hardware ceiling."""
    if "TXPOWER" not in by_name:
        return []
    cap = model_max_txpower(board_name)
    if cap is None:
        return []
    s4 = by_name["TXPOWER"]
    if s4 > cap:
        return [_make_issue(
            severity="error",
            names=("TXPOWER",),
            name_to_sreg=name_to_sreg,
            title=f"TXPOWER {s4} dBm exceeds {board_name} maximum ({cap} dBm)",
            detail="The radio chip clips silently above this — the actual "
                   "output won't match the configured value.",
            fix_hint=f"Set TXPOWER to {cap} or lower.",
            suggested_value=cap,
        )]
    return []


def _rule_5(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
) -> list[ValidationIssue]:
    """R5 — 1 W output requires more current than a typical UART supplies."""
    if by_name.get("TXPOWER") != 30:
        return []
    return [_make_issue(
        severity="info",
        names=("TXPOWER",),
        name_to_sreg=name_to_sreg,
        title="1 W output requires external power",
        detail="30 dBm peak draws ≈2 A briefly; the typical autopilot "
               "telemetry port can't supply this. Use a separate ≥2 A "
               "regulator.",
    )]


def _rule_7(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
    board_name: str,
) -> list[ValidationIssue]:
    """R7 — newer air rates (200/224 kbps) aren't in the 8051 SiK table."""
    if "AIR_SPEED" not in by_name:
        return []
    s2 = by_name["AIR_SPEED"]
    if s2 not in (200, 224):
        return []
    fam = model_family(board_name)
    if fam is None or fam.code != "8051":
        return []
    return [_make_issue(
        severity="warning",
        names=("AIR_SPEED",),
        name_to_sreg=name_to_sreg,
        title=f"AIR_SPEED {s2} not in original SiK rate table",
        detail="The 8051 SiK firmware on this board may not support this "
               "air rate. Use 64 or 250 instead.",
    )]


def _rule_8(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
    region: Region | None,
) -> list[ValidationIssue]:
    """R8 — frequency range matches no known regulatory region."""
    s8 = by_name.get("MIN_FREQ", 0)
    s9 = by_name.get("MAX_FREQ", 0)
    if s8 <= 0 or s9 <= 0:
        return []
    if region is not None:
        return []
    return [_make_issue(
        severity="warning",
        names=("MIN_FREQ", "MAX_FREQ"),
        name_to_sreg=name_to_sreg,
        title="Frequency range doesn't match any known region",
        detail="Verify legality with your local spectrum regulator before "
               "transmitting.",
    )]


def _rule_9(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
    region: Region | None,
) -> list[ValidationIssue]:
    """R9 — NUM_CHANNELS below regional FHSS minimum."""
    if region is None or region.min_channels is None:
        return []
    if "NUM_CHANNELS" not in by_name:
        return []
    s10 = by_name["NUM_CHANNELS"]
    if s10 >= region.min_channels:
        return []
    return [_make_issue(
        severity="warning",
        names=("NUM_CHANNELS",),
        name_to_sreg=name_to_sreg,
        title=(
            f"NUM_CHANNELS={s10} below {region.code} minimum "
            f"({region.min_channels})"
        ),
        detail=(
            f"{region.citation} requires at least {region.min_channels} "
            f"channels for FHSS compliance."
        ),
        suggested_value=region.min_channels,
        citation=region.citation,
    )]


def _rule_10(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
    region: Region | None,
) -> list[ValidationIssue]:
    """R10 — TX power above the regulatory limit for the matched region."""
    if region is None or region.max_tx_dbm is None:
        return []
    if "TXPOWER" not in by_name:
        return []
    s4 = by_name["TXPOWER"]
    if s4 <= region.max_tx_dbm:
        return []
    return [_make_issue(
        severity="error",
        names=("TXPOWER",),
        name_to_sreg=name_to_sreg,
        title=(
            f"TXPOWER {s4} dBm exceeds {region.code} regulatory limit "
            f"({region.max_tx_dbm} dBm)"
        ),
        detail=f"{region.citation} caps conducted TX power for this band.",
        suggested_value=region.max_tx_dbm,
        citation=region.citation,
    )]


def _rule_11(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
    region: Region | None,
) -> list[ValidationIssue]:
    """R11 — DUTY_CYCLE exceeds the matched region's maximum."""
    if region is None or region.duty_cycle is None:
        return []
    if "DUTY_CYCLE" not in by_name:
        return []
    s11 = by_name["DUTY_CYCLE"]
    # The region figure is a maximum: a lower duty cycle is always allowed.
    if s11 <= region.duty_cycle:
        return []
    return [_make_issue(
        severity="warning",
        names=("DUTY_CYCLE",),
        name_to_sreg=name_to_sreg,
        title=(
            f"DUTY_CYCLE={s11} exceeds {region.code} "
            f"maximum ({region.duty_cycle}%)"
        ),
        detail=(
            f"{region.citation} mandates a maximum duty cycle of "
            f"{region.duty_cycle}%."
        ),
        suggested_value=region.duty_cycle,
        citation=region.citation,
    )]


def _rule_12(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
    region: Region | None,
) -> list[ValidationIssue]:
    """R12 — LBT_RSSI below the matched region's minimum."""
    if region is None or region.lbt_rssi_min is None:
        return []
    if "LBT_RSSI" not in by_name:
        return []
    s12 = by_name["LBT_RSSI"]
    if s12 >= region.lbt_rssi_min:
        return []
    return [_make_issue(
        severity="warning",
        names=("LBT_RSSI",),
        name_to_sreg=name_to_sreg,
        title=(
            f"LBT_RSSI={s12} below {region.code} minimum "
            f"({region.lbt_rssi_min})"
        ),
        detail=(
            f"{region.citation} requires Listen-Before-Talk; LBT_RSSI "
            f"must be ≥{region.lbt_rssi_min}."
        ),
        suggested_value=region.lbt_rssi_min,
        citation=region.citation,
    )]


def _rule_13(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
) -> list[ValidationIssue]:
    """R13 — Golay ECC enabled (no longer recommended)."""
    if by_name.get("ECC") != 1:
        return []
    return [_make_issue(
        severity="warning",
        names=("ECC",),
        name_to_sreg=name_to_sreg,
        title="ECC enabled (no longer recommended)",
        detail="ArduPilot wiki: 'Using error correction is no longer "
               "recommended due to the range reduction and the fact that "
               "some newer radio chips are not capable of doing ECC.'",
        suggested_value=0,
    )]


def _rule_14(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
) -> list[ValidationIssue]:
    """R14 — MAVLink framing disabled at a telemetry-class rate."""
    if "MAVLINK" not in by_name or "AIR_SPEED" not in by_name:
        return []
    if by_name["MAVLINK"] != 0:
        return []
    if by_name["AIR_SPEED"] not in _TELEMETRY_AIR_RATES:
        return []
    return [_make_issue(
        severity="info",
        names=("MAVLINK",),
        name_to_sreg=name_to_sreg,
        title="MAVLink framing disabled",
        detail="At telemetry-class air rates, MAVLINK=2 (low-latency) "
               "prevents packet fragmentation and prioritises RC_OVERRIDE. "
               "Set MAVLINK=0 only if you're bridging non-MAVLink serial.",
    )]


def _rule_15(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
) -> list[ValidationIssue]:
    """R15 — MAVLink framing on a slow link saturates easily."""
    if "MAVLINK" not in by_name or "AIR_SPEED" not in by_name:
        return []
    s6 = by_name["MAVLINK"]
    s2 = by_name["AIR_SPEED"]
    if s6 < 1 or not (0 < s2 < 16):
        return []
    return [_make_issue(
        severity="warning",
        names=("AIR_SPEED", "MAVLINK"),
        name_to_sreg=name_to_sreg,
        title="MAVLink framing on a slow link",
        detail="Air rates below 16 kbps are easily saturated by full-rate "
               "MAVLink. Reduce telemetry rates or raise AIR_SPEED.",
    )]


def _rule_16(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
) -> list[ValidationIssue]:
    """R16 — small MAX_WINDOW combined with low AIR_SPEED starves throughput."""
    if "MAX_WINDOW" not in by_name or "AIR_SPEED" not in by_name:
        return []
    s15 = by_name["MAX_WINDOW"]
    s2 = by_name["AIR_SPEED"]
    if s15 >= 50 or not (0 < s2 < 64):
        return []
    return [_make_issue(
        severity="warning",
        names=("AIR_SPEED", "MAX_WINDOW"),
        name_to_sreg=name_to_sreg,
        title="Low MAX_WINDOW + low AIR_SPEED starves throughput",
        detail="Small windows minimise latency but require sufficient "
               "air-rate headroom. Either raise AIR_SPEED to ≥64 or "
               "raise MAX_WINDOW.",
    )]


def _rule_17(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
) -> list[ValidationIssue]:
    """R17 — RTS/CTS rarely useful below 64 kbps air rate."""
    if "RTSCTS" not in by_name or "AIR_SPEED" not in by_name:
        return []
    if by_name["RTSCTS"] != 1 or not (0 < by_name["AIR_SPEED"] < 64):
        return []
    return [_make_issue(
        severity="info",
        names=("RTSCTS",),
        name_to_sreg=name_to_sreg,
        title="RTS/CTS most useful at AIR_SPEED ≥ 64 kbps",
        detail="Hardware flow control protects against UART overrun mainly "
               "when the air link approaches the serial speed.",
    )]


def _rule_18(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
) -> list[ValidationIssue]:
    """R18 — using factory NETID risks colliding with another nearby pair."""
    if by_name.get("NETID") != 25:
        return []
    return [_make_issue(
        severity="info",
        names=("NETID",),
        name_to_sreg=name_to_sreg,
        title="Using factory NETID (25)",
        detail="Pairs operating in proximity must each pick a unique NETID; "
               "otherwise hop sequences collide and ranges drop.",
    )]


def _rule_19(
    s_params: dict[int, int],
    current_values: dict[int, int],
    sreg_to_name: dict[int, str],
    firmware_banner: str,
) -> list[ValidationIssue]:
    """R19 — firmware-locked register: pre-flight detection.

    Some RFDesign firmware variants (notably "-US"/"-EU" SKUs) factory-lock
    a subset of S-registers to keep the radio inside its certification
    envelope.  The radio replies "ERROR" to *any* attempted change of those
    registers.  This rule operates in *sreg* space because the lock is
    enforced by the firmware on the wire — but the issue title surfaces
    the firmware's reported parameter NAME so the user sees what the
    register actually represents on their radio.
    """
    locked = regions.firmware_lockdown(firmware_banner)
    if not locked:
        return []
    label = regions.lockdown_label(firmware_banner) or "this firmware variant"
    out: list[ValidationIssue] = []
    for sreg in sorted(locked):
        if sreg not in s_params or sreg not in current_values:
            continue
        intended = s_params[sreg]
        actual = current_values[sreg]
        if intended == actual:
            continue
        name = sreg_to_name.get(sreg, "")
        display = f"S{sreg} {name}".strip() or f"S{sreg}"
        out.append(ValidationIssue(
            severity="warning",
            sregs=(sreg,),
            param_names=(name,) if name else (),
            title=f"{display} is firmware-locked on {label}",
            detail=(
                f"Your intended value ({intended}) differs from the radio's "
                f"current value ({actual}). RFDesign locks frequency-hopping "
                f"and air-rate parameters on certified region SKUs to keep "
                f"the radio inside its FCC/ETSI/ACMA test envelope. Save will "
                f"fail for this register; revert to {actual} or leave it as-is."
            ),
            fix_hint=f"Revert to the radio's locked value ({actual}).",
            suggested_value=actual,
        ))
    return out


# --------------------------------------------------------------------- MultiComm rules
# Rules M1..M16 encode the constraints and cautions in the RFD MultiComm
# Firmware User Manual v1.0 (section numbers cited per rule).  They only
# see one node's configuration; cross-node checks (unique NODEID, disjoint
# SLOTIDS, matching network-scope params) live in the Compare dialog.

_MC_MAN = "RFD MultiComm manual"


def _mc_issue(n2s: dict[str, int], severity: str, names: tuple[str, ...],
              title: str, detail: str, *, fix_hint: str = "",
              suggested_value: ParamValue | None = None,
              section: str = "") -> ValidationIssue:
    return _make_issue(
        severity=severity, names=names, name_to_sreg=n2s, title=title,
        detail=detail, fix_hint=fix_hint, suggested_value=suggested_value,
        citation=f"{_MC_MAN} §{section}" if section else "",
    )


def _mc_rf(by: dict, n2s: dict) -> list[ValidationIssue]:
    out: list[ValidationIssue] = []
    fmin, fmax = by.get("FREQMIN"), by.get("FREQMAX")
    nch, rate = by.get("NUMCHANS"), by.get("BITRATE")
    if isinstance(fmin, int) and isinstance(fmax, int):
        # M1 — band order
        if fmin >= fmax:
            out.append(_mc_issue(
                n2s, "error", ("FREQMIN", "FREQMAX"), "FREQMIN ≥ FREQMAX",
                f"The hopping band is empty ({fmin}–{fmax} kHz).",
                fix_hint="Set FREQMIN below FREQMAX.", section="4.2.5"))
        # M2 — inside a supported band
        elif not (902000 <= fmin and fmax <= 928000) and not (
            865000 <= fmin and fmax <= 870000
        ):
            out.append(_mc_issue(
                n2s, "warning", ("FREQMIN", "FREQMAX"), "Band outside 868/900 MHz",
                f"{fmin}–{fmax} kHz isn't inside 902–928 MHz or 865–870 MHz; "
                "the radio will reject or clamp it.", section="4.1"))
        # M3 — channel spacing
        elif isinstance(nch, int) and isinstance(rate, int) and nch > 0:
            spacing = mc.channel_spacing_khz(fmin, fmax, nch)
            need = mc.required_spacing_khz(rate)
            if spacing < need:
                best = max(1, int((fmax - fmin) / need) - 1)
                out.append(_mc_issue(
                    n2s, "warning", ("NUMCHANS", "FREQMIN", "FREQMAX", "BITRATE"),
                    "Channels too close together",
                    f"{nch} channels in {fmin}–{fmax} kHz gives {spacing:.0f} kHz "
                    f"spacing; {rate} kbps needs at least {need} kHz.",
                    fix_hint=f"Use NUMCHANS ≤ {best}.", suggested_value=best,
                    section="4.2.5"))
    # M4 — slot size window
    tus = by.get("TOTALUS")
    if isinstance(tus, int) and isinstance(rate, int) and rate > 0:
        size = mc.slot_bytes(tus, rate)
        if size < mc.SLOT_BYTES_MIN or size > mc.SLOT_BYTES_MAX:
            target = mc.SLOT_BYTES_MIN if size < mc.SLOT_BYTES_MIN else mc.SLOT_BYTES_MAX
            fix = int(target * 8000 / rate) + (1 if size < mc.SLOT_BYTES_MIN else 0)
            achievable = 5000 <= fix <= 220000
            out.append(_mc_issue(
                n2s, "warning", ("TOTALUS", "BITRATE"), "Slot size out of range",
                f"TOTALUS {tus} µs at {rate} kbps ≈ {size:.0f} bytes per slot; "
                "the firmware needs ~300–1000 and will silently change TOTALUS "
                "on reboot — then it may no longer match the other nodes.",
                fix_hint=(
                    f"Set TOTALUS to {fix} on every node." if achievable else
                    f"No TOTALUS in 5000–220000 µs gives a valid slot at {rate} "
                    "kbps; the firmware will pick one — check it matches on "
                    "every node."
                ),
                suggested_value=fix if achievable else None, section="2.5.1"))
    # M5 — long range mode
    if by.get("XTRALONGRNG") == 1:
        out.append(_mc_issue(
            n2s, "info", ("XTRALONGRNG",), "Extra-long-range mode on",
            "Only for links beyond ~40 km; it noticeably reduces throughput.",
            section="4.2.5"))
    return out


def _mc_network(by: dict, n2s: dict) -> list[ValidationIssue]:
    out: list[ValidationIssue] = []
    slots, mask = by.get("SLOTS"), by.get("SLOTIDS")
    auto, node = by.get("P2PAUTO"), by.get("NODEID")
    sub, subs = by.get("SUBNETID"), by.get("SUBNETS")
    txmode = by.get("SLOTTXMODE")
    # M6 — owned slots must exist
    if isinstance(slots, int) and isinstance(mask, int):
        beyond = [b for b in mc.slots_in(mask) if b >= slots]
        if beyond:
            fixed = mask & ((1 << slots) - 1)
            out.append(_mc_issue(
                n2s, "error", ("SLOTIDS", "SLOTS"), "SLOTIDS uses non-existent slots",
                f"SLOTIDS owns {mc.describe_slots(mask)} but there are only "
                f"{slots} slot(s) (bits 0..{slots - 1}).",
                fix_hint=f"Clear bits ≥ {slots} (SLOTIDS={fixed}) or raise SLOTS.",
                suggested_value=fixed, section="4.2.5"))
        if mask == 0 and txmode not in (0, None):
            out.append(_mc_issue(
                n2s, "warning", ("SLOTIDS", "SLOTTXMODE"), "Node owns no slots",
                "SLOTIDS is 0 so this node can never transmit its own data. "
                "For a listen-only node set SLOTTXMODE=0 instead.",
                section="4.2.5"))
    # M7 — master owns slot 1; others shouldn't
    if auto == 0 and isinstance(mask, int):
        if mc.is_master(by) and not mask & 1:
            out.append(_mc_issue(
                n2s, "error", ("SLOTIDS", "NODEID"), "Master must own slot 1",
                "In fixed-master mode NODEID 0 / SUBNETID 0 is the master, and "
                "its SLOTIDS must include bit 0.",
                fix_hint=f"Set SLOTIDS={mask | 1}.", suggested_value=mask | 1,
                section="2.5.2.3"))
        elif not mc.is_master(by) and mask & 1 and by.get("SUBNETID", 0) == 0:
            out.append(_mc_issue(
                n2s, "warning", ("SLOTIDS",), "Non-master node owns slot 1",
                "Slot 1 (bit 0) belongs to the master. Two nodes owning the "
                "same slot will collide.", fix_hint=f"Use SLOTIDS={mask & ~1 or 2}.",
                suggested_value=(mask & ~1) or 2, section="4.2.5"))
    # M7b — the master drives the network's sync: it must transmit there
    if mc.is_master(by) and isinstance(txmode, int):
        if txmode == 0:
            out.append(_mc_issue(
                n2s, "error", ("NODEID", "SLOTTXMODE"), "Listen-only node is the master",
                "NODEID 0 in SUBNETID 0 with P2PAUTO=0 is the master, but "
                "SLOTTXMODE=0 means it never transmits, so the network can't "
                "sync. Give a listen-only node its own NODEID.",
                section="2.6"))
        elif not txmode & 0b100:
            out.append(_mc_issue(
                n2s, "error", ("SLOTTXMODE",), "Master can't transmit sync",
                "SLOTTXMODE bit 2 (sync slot) is cleared on the master, so no "
                "node will synchronise.", suggested_value=txmode | 0b100,
                section="4.2.5"))
    # M8 — automatic master is for pairs only
    if auto == 1:
        if isinstance(slots, int) and slots > 2:
            out.append(_mc_issue(
                n2s, "warning", ("P2PAUTO", "SLOTS"), "Auto master with >2 slots",
                "Automatic master linking only works for a pair of modems; "
                "networks must use P2PAUTO=0 with unique NODEIDs.",
                fix_hint="Set P2PAUTO=0.", suggested_value=0, section="4.2.5"))
        if txmode == 0:
            out.append(_mc_issue(
                n2s, "warning", ("P2PAUTO", "SLOTTXMODE"),
                "Listen-only node needs a fixed master",
                "Listen-only nodes can only join networks with P2PAUTO=0.",
                suggested_value=0, section="2.6"))
    # M9 — subnet bounds
    if isinstance(sub, int) and isinstance(subs, int) and sub >= subs:
        # SUBNETS first: Apply-fix targets the first register listed.
        out.append(_mc_issue(
            n2s, "error", ("SUBNETS", "SUBNETID"), "SUBNETID ≥ SUBNETS",
            f"Valid SUBNETIDs are 0..{subs - 1}; this node would never sync.",
            fix_hint=f"Set SUBNETS to at least {sub + 1} on every node.",
            suggested_value=sub + 1, section="4.2.2"))
    nch = by.get("NUMCHANS")
    if isinstance(subs, int) and isinstance(nch, int) and subs > 1 and subs > nch - 1:
        out.append(_mc_issue(
            n2s, "warning", ("SUBNETS", "NUMCHANS"), "More subnets than channels allow",
            f"With {nch} channels the practical maximum is {max(nch - 1, 1)} subnets.",
            section="4.2.2"))
    # M10 — bind ID is developmental
    if isinstance(by.get("BINDID"), int) and by["BINDID"] != 0:
        out.append(_mc_issue(
            n2s, "warning", ("BINDID",), "BINDID is developmental",
            "RFDesign advises not using BINDID; separate networks by frequency "
            "band instead.", suggested_value=0, section="4.2.2"))
    # M11 — SLOTTXMODE bit 0
    if isinstance(txmode, int) and txmode != 0 and not txmode & 1:
        out.append(_mc_issue(
            n2s, "warning", ("SLOTTXMODE",), "SLOTTXMODE bit 0 cleared",
            "Bit 0 (transmit in own slots) should always be set unless the "
            "node is listen-only (SLOTTXMODE=0).",
            suggested_value=txmode | 1, section="4.2.5"))
    if isinstance(node, int) and auto == 0 and node == 0 and isinstance(sub, int) and sub > 0:
        out.append(_mc_issue(
            n2s, "info", ("NODEID", "SUBNETID"), "NODEID 0 outside subnet 0",
            "Only NODEID 0 in SUBNETID 0 is the master; this node is an "
            "ordinary member of its subnetwork.", section="2.5.2.3"))
    return out


def _mc_routing(by: dict, n2s: dict) -> list[ValidationIssue]:
    out: list[ValidationIssue] = []
    dest, node = by.get("DESTID"), by.get("NODEID")
    # M12 — destination
    if isinstance(dest, int):
        if dest >= 64:
            out.append(_mc_issue(
                n2s, "error", ("DESTID",), "DESTID 64 is reserved",
                "Use 0–62 for a specific node or 63 to broadcast.",
                suggested_value=mc.BROADCAST_DESTID, section="4.2.2"))
        elif dest == node and by.get("P2PAUTO") == 0:
            out.append(_mc_issue(
                n2s, "warning", ("DESTID", "NODEID"), "Node sends to itself",
                f"DESTID equals this node's NODEID ({node}); its serial data "
                "won't reach anyone.", suggested_value=mc.BROADCAST_DESTID,
                section="4.2.2"))
    # M13 — debug hop filters
    lo, hi = by.get("VALIDHOPSMIN"), by.get("VALIDHOPSMAX")
    if isinstance(lo, int) and isinstance(hi, int):
        if lo > hi:
            out.append(_mc_issue(
                n2s, "error", ("VALIDHOPSMIN", "VALIDHOPSMAX"),
                "VALIDHOPSMIN > VALIDHOPSMAX", "Every packet would be dropped.",
                section="4.2.2"))
        elif lo != 0 or hi != 7:
            out.append(_mc_issue(
                n2s, "info", ("VALIDHOPSMIN", "VALIDHOPSMAX"),
                "Hop filters set (bench-test mode)",
                f"VALIDHOPS {lo}..{hi}: these are for forcing hops on the bench. "
                "Normal operation needs 0..7.", section="4.2.2"))
    fwd, hops = by.get("FWDTABLE"), by.get("HOPSMAX")
    if isinstance(fwd, tuple) and any(v != 64 for v in fwd[0::2]) and hops == 1:
        out.append(_mc_issue(
            n2s, "warning", ("FWDTABLE", "HOPSMAX"), "Forwarding with HOPSMAX=1",
            "FWDTABLE has entries but HOPSMAX is 1, so relayed packets can't "
            "take an extra hop. Set HOPSMAX on every node to the hop count.",
            section="5.3.3"))
    return out


def _mc_encryption(by: dict, n2s: dict) -> list[ValidationIssue]:
    out: list[ValidationIssue] = []
    lvl, key = by.get("ENCRYPTLVL"), by.get("ENCRYPTKEY")
    # M14 — key shape and strength
    if isinstance(key, tuple) and len(key) != 32:
        out.append(_mc_issue(
            n2s, "error", ("ENCRYPTKEY",), "Key must be 32 bytes",
            f"ENCRYPTKEY has {len(key)} bytes; all 32 are required even for "
            "AES-128.", section="2.4"))
    if isinstance(lvl, int) and lvl > 0 and isinstance(key, tuple) and (
        key in mc.PUBLISHED_ENCRYPTKEYS
    ):
        out.append(_mc_issue(
            n2s, "warning", ("ENCRYPTKEY", "ENCRYPTLVL"), "Encryption uses the default key",
            "The factory key is printed in the public manual, so this link is "
            "effectively unencrypted. Set your own 32-byte key on every node.",
            section="2.4"))
    return out


def _mc_serial_and_pins(
    by: dict, n2s: dict, pin_names: list[str],
    allowed_pin_funcs: dict[str, frozenset[int]],
) -> list[ValidationIssue]:
    out: list[ValidationIssue] = []
    funcs: dict[str, int] = {
        n: by[n] for n in pin_names if isinstance(by.get(n), int)
    }
    assigned = set(funcs.values())

    # M15 — pin function sanity
    for name, f in funcs.items():
        allowed = allowed_pin_funcs.get(name) or mc.ALLOWED_PIN_FUNCS.get(name)
        if allowed is not None and f not in allowed:
            out.append(_mc_issue(
                n2s, "error", (name,),
                f"{name}: {mc.PIN_FUNCTIONS.get(f, f)} not allowed on this pin",
                "The firmware's allowed-function mask for this pin excludes it; "
                "the write will be rejected.", suggested_value=0, section="7.1"))
        elif f in mc.RESERVED_PIN_FUNCS:
            out.append(_mc_issue(
                n2s, "error", (name,), f"{name}: reserved function {f}",
                "Value 8 is reserved.", suggested_value=0, section="4.2.6"))
        elif f in mc.INTERNAL_PIN_FUNCS:
            out.append(_mc_issue(
                n2s, "warning", (name,),
                f"{name}: {mc.PIN_FUNCTIONS.get(f, f)}",
                "Functions 22 and up are not intended for user functions.",
                section="4.2.6"))
    for f in mc.UNIQUE_PIN_FUNCS:
        holders = [n for n, v in funcs.items() if v == f]
        if len(holders) > 1:
            out.append(_mc_issue(
                n2s, "error", tuple(holders),
                f"{mc.PIN_FUNCTIONS[f]} on several pins",
                f"{', '.join(holders)} are all set to {mc.PIN_FUNCTIONS[f]}.",
                section="4.2.6"))
    rc_in = assigned & mc.RC_INPUT_FUNCS
    rc_out = assigned & mc.RC_OUTPUT_FUNCS
    if rc_in and rc_out:
        out.append(_mc_issue(
            n2s, "error", tuple(n for n, v in funcs.items()
                               if v in mc.RC_INPUT_FUNCS | mc.RC_OUTPUT_FUNCS),
            "RC input and output on one node",
            "A node is either the RC input (ground) or RC output (vehicle) side.",
            section="2.5.1.2"))
    if rc_in and by.get("PRI") != 3:
        out.append(_mc_issue(
            n2s, "warning", ("PRI",), "RC input node should be PRI=3",
            "Priority 3 lets the RC input take air time from other nodes and "
            "keeps control latency low.", suggested_value=3, section="2.5.1.2"))
    if (rc_in or rc_out) and isinstance(by.get("SLOTS"), int) and by["SLOTS"] > 2:
        out.append(_mc_issue(
            n2s, "warning", ("SLOTS",), "RC passthrough outside point-to-point",
            "PPM/SBUS passthrough is a point-to-point feature (SLOTS=2).",
            section="1"))

    # M16 — serial ports
    if by.get("S1KBPS") == 0:
        out.append(_mc_issue(
            n2s, "warning", ("S1KBPS",), "Serial port 1 disabled",
            "S1KBPS=0 turns off the main serial port — host data on port 1 "
            "will no longer be sent.", suggested_value=1000, section="4.2.4"))
    for port, kbps, rts, cts in (
        ("1", "S1KBPS", mc.PIN_FUNC_RTS, mc.PIN_FUNC_CTS),
        ("2", "S2KBPS", mc.PIN_FUNC_AUXRTS, mc.PIN_FUNC_AUXCTS),
    ):
        k = by.get(kbps)
        if isinstance(k, int) and 0 < k < 1000 and funcs and not (
            rts in assigned and cts in assigned
        ):
            out.append(_mc_issue(
                n2s, "warning", (kbps,), f"Port {port} limit without flow control",
                f"A bandwidth limit on port {port} needs RTS/CTS pins configured "
                "to work properly.", section="4.2.4"))
    # Local port 2 is in play when it boots in AT mode or port 1 input is
    # echoed to it.  (RXROUTE 1/3 target the *receiving* node's port 2.)
    port2_used = by.get("S2ATMODE") == 1 or by.get("S1RXROUTE") in (2, 3)
    if funcs and port2_used and not (
        mc.PIN_FUNC_AUXTX in assigned and mc.PIN_FUNC_AUXRX in assigned
    ):
        out.append(_mc_issue(
            n2s, "warning", tuple(n for n in ("S2ATMODE", "S1RXROUTE") if n in by),
            "Serial port 2 used but pins not assigned",
            "Port 2 needs one pin set to AUX TX (18) and one to AUX RX (19) "
            "(defaults: GPIO5 / GPIO4).", section="4.2.4"))
    # Pin mirror map
    mirror = by.get("PINMIRROR")
    if isinstance(mirror, tuple):
        mirror_inputs = [
            i for i, (n, *_r) in enumerate(mc.PIN_REGISTERS)
            if funcs.get(n) == mc.PIN_FUNC_MIRROR and i < len(mirror)
        ]
        targets = [mirror[i] for i in mirror_inputs]
        if len(targets) != len(set(targets)):
            out.append(_mc_issue(
                n2s, "error", ("PINMIRROR",), "Two inputs mirror to one output",
                "Each MIRROR input must map to a different remote pin.",
                section="4.2.6"))
    return out


def _mc_hardware(by: dict, n2s: dict, board_name: str) -> list[ValidationIssue]:
    fam = model_family(board_name)
    if fam is None or fam.code == "stm32_v2":
        return []
    return [_mc_issue(
        n2s, "warning", (), "MultiComm needs V2 hardware",
        f"{board_name} isn't an x2/ux2 modem; MultiComm only supports "
        "868x2, 868ux2, 900x2 and 900ux2. (A 1 s on / 3 s off green LED "
        "means unsupported hardware.)", section="2.1.1")]


def _mc_ranges(
    by: dict, n2s: dict, meta: dict[int, registers.ParamMeta] | None,
) -> list[ValidationIssue]:
    """M0 — every value within the radio-reported (else catalog) limits.
    Without this, Validate could say "all pass" for values Save rejects."""
    out: list[ValidationIssue] = []
    for name, value in by.items():
        sreg = n2s.get(name)
        if sreg is None:
            continue
        reg = registers.derive_def(
            sreg, name, family=FAMILY_MULTICOMM, meta=(meta or {}).get(sreg),
        )
        if reg.read_only:
            continue
        ok, why = registers.validate_def(reg, value)
        if not ok:
            out.append(_mc_issue(
                n2s, "error", (name,), f"{name} out of range",
                why + ". The radio will reject this value (ERROR2).",
                suggested_value=reg.default if reg.default is not None
                and registers.validate_def(reg, reg.default)[0] else None,
                section="4.1"))
    return out


def _validate_multicomm(
    by_name: dict[str, ParamValue],
    name_to_sreg: dict[str, int],
    *,
    board_name: str,
    is_remote: bool,
    meta: dict[int, registers.ParamMeta] | None = None,
) -> list[ValidationIssue]:
    pin_names = [n for n, *_r in mc.PIN_REGISTERS]
    # Prefer the radio's own per-pin allowed-function masks when reported.
    allowed_pin_funcs: dict[str, frozenset[int]] = {}
    for name in pin_names:
        m = (meta or {}).get(name_to_sreg.get(name, -1))
        if m is not None and m.mask:
            allowed_pin_funcs[name] = frozenset(i for i in range(31) if m.mask >> i & 1)
    issues: list[ValidationIssue] = []
    issues.extend(_mc_ranges(by_name, name_to_sreg, meta))
    issues.extend(_mc_rf(by_name, name_to_sreg))
    issues.extend(_mc_network(by_name, name_to_sreg))
    issues.extend(_mc_routing(by_name, name_to_sreg))
    issues.extend(_mc_encryption(by_name, name_to_sreg))
    issues.extend(_mc_serial_and_pins(by_name, name_to_sreg, pin_names, allowed_pin_funcs))
    if not is_remote:
        issues.extend(_mc_hardware(by_name, name_to_sreg, board_name))
    return issues


# --------------------------------------------------------------------- entry point

def validate_config(
    s_params: dict[int, ParamValue],
    *,
    sreg_to_name: dict[int, str] | None = None,
    board_name: str = "",
    pin_params: dict[int, int] | None = None,
    is_remote: bool = False,
    firmware_banner: str = "",
    current_values: dict[int, ParamValue] | None = None,
    family: str | None = None,
    meta: dict[int, registers.ParamMeta] | None = None,
) -> ValidationReport:
    """Run every rule in canonical order and return a :class:`ValidationReport`.

    ``s_params`` and ``current_values`` are sreg-keyed (matching what the
    UI tracks).  ``sreg_to_name`` tells us which sreg holds which parameter
    on this firmware — typically the ``s_names`` field of the radio's
    ``ATI5`` response.  When omitted the canonical SiK mapping is used,
    which matches the original firmware but not RFDesign's SiK 3.x.

    ``firmware_banner`` and ``current_values`` are inputs to R19
    (firmware-lockdown detection); without them R19 is silently skipped.

    Set ``is_remote=True`` when validating the *remote* radio's panel —
    model-specific rules (R4, R5, R7, R19) are skipped because the partner
    radio's firmware variant isn't reliably known.

    ``family`` selects the rule set; when None it's inferred from the
    parameter names.  MultiComm configurations get rules M1..M16 instead
    of the SiK rules; ``meta`` (radio-reported constraints, sreg-keyed)
    sharpens them where available.
    """
    if sreg_to_name is None:
        sreg_to_name = registers.CANONICAL_SIK_NAMES
    name_to_sreg: dict[str, int] = {n: s for s, n in sreg_to_name.items()}

    # Project sreg-keyed inputs onto the name-keyed view rules use.
    by_name: dict[str, ParamValue] = {}
    for sreg, value in s_params.items():
        name = sreg_to_name.get(sreg)
        if name:
            by_name[name] = value

    if family is None and mc.looks_like_multicomm(names=sreg_to_name.values()):
        family = FAMILY_MULTICOMM
    if family == FAMILY_MULTICOMM:
        fmin, fmax = by_name.get("FREQMIN"), by_name.get("FREQMAX")
        mc_region = (
            detect_region(fmin, fmax)
            if isinstance(fmin, int) and isinstance(fmax, int) else None
        )
        return ValidationReport(
            issues=tuple(_validate_multicomm(
                by_name, name_to_sreg, board_name=board_name, is_remote=is_remote,
                meta=meta,
            )),
            detected_region=mc_region,
            detected_board=board_name,
        )

    region: Region | None = None
    if "MIN_FREQ" in by_name and "MAX_FREQ" in by_name:
        region = detect_region(by_name["MIN_FREQ"], by_name["MAX_FREQ"])

    issues: list[ValidationIssue] = []
    issues.extend(_rule_1(by_name, name_to_sreg))
    issues.extend(_rule_2(by_name, name_to_sreg))
    issues.extend(_rule_3(by_name, name_to_sreg))
    if not is_remote:
        issues.extend(_rule_4(by_name, name_to_sreg, board_name))
        issues.extend(_rule_5(by_name, name_to_sreg))
        issues.extend(_rule_7(by_name, name_to_sreg, board_name))
    issues.extend(_rule_8(by_name, name_to_sreg, region))
    issues.extend(_rule_9(by_name, name_to_sreg, region))
    issues.extend(_rule_10(by_name, name_to_sreg, region))
    issues.extend(_rule_11(by_name, name_to_sreg, region))
    issues.extend(_rule_12(by_name, name_to_sreg, region))
    issues.extend(_rule_13(by_name, name_to_sreg))
    issues.extend(_rule_14(by_name, name_to_sreg))
    issues.extend(_rule_15(by_name, name_to_sreg))
    issues.extend(_rule_16(by_name, name_to_sreg))
    issues.extend(_rule_17(by_name, name_to_sreg))
    issues.extend(_rule_18(by_name, name_to_sreg))
    if not is_remote and current_values:
        issues.extend(_rule_19(
            dict(s_params), dict(current_values), sreg_to_name, firmware_banner,
        ))

    return ValidationReport(
        issues=tuple(issues),
        detected_region=region,
        detected_board=board_name,
    )

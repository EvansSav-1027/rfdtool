"""Pure-logic configuration diff used by the Compare dialog and the
pairing wizard.  No Qt — this is testable without a QApplication.

The diff is **name-keyed** so it works correctly across firmware variants
that put parameters at different sreg numbers (e.g. RFDesign 3.x putting
MAX_WINDOW at S14 while canonical SiK has it at S15).

Each entry carries the sreg number on each side as well, so the UI can
tint specific rows in the panel even if the two sides don't agree on
where a parameter lives.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import multicomm as mc
from .registers import FAMILY_MULTICOMM, ParamValue


# Severity classes used to colour rows in the Compare dialog and to rank
# mismatches in the pairing wizard's diagnostic.
#
# - "rf"     — RF-critical: mismatch prevents a clean link or causes
#              partial-link symptoms (RSSI > 0 but pkts = 0).
# - "soft"   — Affects link quality but typically not the bind itself.
# - "uart"   — UART/GPIO/diagnostic; doesn't affect on-air link.
# - "missing-a" / "missing-b" — Parameter exists on one side but not the
#              other (e.g. canonical SiK MANCHESTER absent on RFDesign 3.x).
#              Treated as informational; the diff dialog renders these
#              with a "skip" hint rather than an error.
# - "conflict" — MultiComm peers that collide: same NODEID or overlapping
#              SLOTIDS within one subnetwork.  Worse than a mismatch.
# - "per-node" — MultiComm per-node setting that legitimately differs
#              between two nodes (NODEID, DESTID, PRI…).  Informational.

SEVERITY_CONFLICT = "conflict"
SEVERITY_PER_NODE = "per-node"
SEVERITY_RF = "rf"
SEVERITY_SOFT = "soft"
SEVERITY_UART = "uart"
SEVERITY_OK = "ok"
SEVERITY_MISSING_A = "missing-a"
SEVERITY_MISSING_B = "missing-b"


# Parameter name → severity if mismatched.  Anything not listed defaults
# to UART (non-RF).
SEVERITY_BY_NAME: dict[str, str] = {
    # RF-critical: must match end-to-end for clean bind.
    "NETID": SEVERITY_RF,
    "AIR_SPEED": SEVERITY_RF,
    "MIN_FREQ": SEVERITY_RF,
    "MAX_FREQ": SEVERITY_RF,
    "NUM_CHANNELS": SEVERITY_RF,
    "DUTY_CYCLE": SEVERITY_RF,
    "ECC": SEVERITY_RF,
    "MANCHESTER": SEVERITY_RF,
    "MAX_WINDOW": SEVERITY_RF,
    # ArduPilot SiK docs list LBT_RSSI among the must-match settings.
    "LBT_RSSI": SEVERITY_RF,
    # Mismatched encryption = RSSI fine but no data: the very symptom the
    # pairing diagnostic exists to explain.
    "ENCRYPTION_LEVEL": SEVERITY_RF,
    # Soft: affects link quality / behaviour but not the initial sync.
    "RTSCTS": SEVERITY_SOFT,
    "OPPRESEND": SEVERITY_SOFT,
    # MAVLink framing is local packetisation; ends may differ.
    "MAVLINK": SEVERITY_SOFT,
    "AIR_FRAMELEN": SEVERITY_SOFT,
    "TXPOWER": SEVERITY_SOFT,
    # UART / GPIO / diagnostic — explicitly listed for clarity even where
    # the default would already classify them correctly.
    "SERIAL_SPEED": SEVERITY_UART,
    "AUXSER_SPEED": SEVERITY_UART,
    "FSFRAMELOSS": SEVERITY_UART,
    "RSSI_IN_DBM": SEVERITY_UART,
    "ANT_MODE": SEVERITY_UART,
    "FORMAT": SEVERITY_UART,
}


def severity_for(name: str) -> str:
    """Severity of a *mismatch* on parameter `name`.  Default: UART."""
    if name.startswith("GPI") or name.startswith("GPO"):
        return SEVERITY_UART
    return SEVERITY_BY_NAME.get(name, SEVERITY_UART)


# Severity ordering used to sort and colour rows.  Higher = more urgent.
_SEVERITY_RANK = {
    SEVERITY_CONFLICT: 5,
    SEVERITY_RF: 4,
    SEVERITY_MISSING_A: 3,
    SEVERITY_MISSING_B: 3,
    SEVERITY_SOFT: 2,
    SEVERITY_UART: 1,
    SEVERITY_PER_NODE: 0,
    SEVERITY_OK: 0,
}


_MC_UART_GROUPS = frozenset({mc.GROUP_SERIAL1, mc.GROUP_SERIAL2, mc.GROUP_GPIO, mc.GROUP_RC})


def multicomm_severity_for(name: str, *, peer: bool) -> str:
    """Severity of a mismatch on MultiComm parameter `name`.

    Parameters the manual marks "same for all nodes" are RF-critical.
    Per-node parameters are expected to differ between two nodes
    (``peer=True``); against a reference for the *same* node they're
    ordinary soft/UART mismatches.
    """
    canon = mc.canonical_name(name) or name
    spec = mc.SPECS.get(mc.NAME_TO_SREG.get(canon, -1))
    if spec is None:
        return SEVERITY_UART
    if canon == "TXPOWER":
        return SEVERITY_SOFT
    if spec.scope == "network":
        return SEVERITY_RF
    if peer:
        return SEVERITY_PER_NODE
    return SEVERITY_UART if spec.group in _MC_UART_GROUPS else SEVERITY_SOFT


def _multicomm_conflicts(
    a: dict[str, ParamValue], b: dict[str, ParamValue],
) -> dict[str, str]:
    """name → SEVERITY_CONFLICT for collisions between two peer nodes."""
    out: dict[str, str] = {}
    if a.get("P2PAUTO") != 0 or b.get("P2PAUTO") != 0:
        return out   # auto-master pairs self-assign; identical configs are normal
    if a.get("SUBNETID", 0) != b.get("SUBNETID", 0):
        return out   # §5.3.2 reuses NODEID/SLOTIDS across subnetworks
    if "NODEID" in a and a.get("NODEID") == b.get("NODEID"):
        out["NODEID"] = SEVERITY_CONFLICT
    sa, sb = a.get("SLOTIDS"), b.get("SLOTIDS")
    if isinstance(sa, int) and isinstance(sb, int) and sa & sb:
        out["SLOTIDS"] = SEVERITY_CONFLICT
    return out


@dataclass(frozen=True)
class CompareEntry:
    name: str                     # parameter name (canonical)
    severity: str                 # see SEVERITY_* constants
    sreg_a: int | None            # sreg on side A (None if missing)
    sreg_b: int | None            # sreg on side B (None if missing)
    value_a: ParamValue | None
    value_b: ParamValue | None

    @property
    def is_match(self) -> bool:
        return self.severity in (SEVERITY_OK, SEVERITY_PER_NODE)

    @property
    def is_conflict(self) -> bool:
        return self.severity == SEVERITY_CONFLICT

    @property
    def is_rf_critical(self) -> bool:
        return self.severity == SEVERITY_RF

    @property
    def is_missing(self) -> bool:
        return self.severity in (SEVERITY_MISSING_A, SEVERITY_MISSING_B)


def diff_configs(
    a_params: dict[str, ParamValue],
    b_params: dict[str, ParamValue],
    *,
    a_sregs: dict[str, int] | None = None,
    b_sregs: dict[str, int] | None = None,
    family: str | None = None,
    peer: bool = False,
) -> list[CompareEntry]:
    """Compute a name-keyed diff between two configurations.

    ``a_params`` / ``b_params`` map parameter name → value.  The optional
    ``a_sregs`` / ``b_sregs`` (name → sreg) let the UI know which row to
    highlight on each side.  When omitted, the entry's ``sreg_a`` /
    ``sreg_b`` will be None.

    ``family`` (inferred from the names when None) switches MultiComm
    severity rules on.  ``peer=True`` says side B is a *different node*
    (e.g. the live remote), so per-node differences are expected and
    NODEID/SLOTIDS collisions are flagged as conflicts.

    Result is sorted by severity (most urgent first), then alphabetically
    by name.
    """
    a_sregs = dict(a_sregs or {})
    b_sregs = dict(b_sregs or {})
    all_names = sorted(set(a_params.keys()) | set(b_params.keys()))
    if family is None and mc.looks_like_multicomm(names=all_names):
        family = FAMILY_MULTICOMM
    is_mc = family == FAMILY_MULTICOMM
    conflicts = _multicomm_conflicts(a_params, b_params) if is_mc and peer else {}

    out: list[CompareEntry] = []
    for name in all_names:
        in_a = name in a_params
        in_b = name in b_params
        sreg_a = a_sregs.get(name)
        sreg_b = b_sregs.get(name)
        value_a = a_params.get(name)
        value_b = b_params.get(name)
        if in_a and not in_b:
            severity = SEVERITY_MISSING_B
        elif in_b and not in_a:
            severity = SEVERITY_MISSING_A
        elif name in conflicts:
            severity = conflicts[name]
        elif value_a == value_b:
            severity = SEVERITY_OK
        elif is_mc:
            severity = multicomm_severity_for(name, peer=peer)
        else:
            severity = severity_for(name)
        out.append(CompareEntry(
            name=name, severity=severity,
            sreg_a=sreg_a, sreg_b=sreg_b,
            value_a=value_a, value_b=value_b,
        ))

    out.sort(key=lambda e: (-_SEVERITY_RANK.get(e.severity, 0), e.name))
    return out


def summarise(entries: list[CompareEntry]) -> dict[str, int]:
    """Quick count of entries per severity for a header line."""
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.severity] = counts.get(e.severity, 0) + 1
    return counts

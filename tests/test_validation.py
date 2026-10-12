"""Unit tests for rfd.validation.

One test per rule (R1..R18, skipping R6 which doesn't exist in the spec)
plus a handful of integration tests covering ``ValidationReport``'s
aggregations and the ``is_remote`` path.
"""
from __future__ import annotations

import pytest

from rfd.regions import find_region
from rfd.validation import (
    ValidationIssue,
    ValidationReport,
    validate_config,
)


# ------------------------------------------------------------ helpers

def _us_base() -> dict[int, int]:
    """A minimal, sane US-region config used as a baseline by several tests."""
    return {
        2: 64,
        3: 26,        # avoid R18
        4: 20,
        5: 0,
        6: 2,
        8: 902000,
        9: 928000,
        10: 50,
        11: 100,
        12: 0,
        14: 0,
        15: 131,
    }


def _titles(report: ValidationReport) -> list[str]:
    return [i.title for i in report.issues]


def _by_sreg(report: ValidationReport, sreg: int) -> list[ValidationIssue]:
    return [i for i in report.issues if sreg in i.sregs]


# ------------------------------------------------------------ R1

def test_R1_invalid_value_produces_error():
    report = validate_config({2: 999})
    matches = [i for i in report.errors if i.sregs == (2,) and "invalid" in i.title]
    assert len(matches) == 1


# ------------------------------------------------------------ R2

def test_R2_inverted_freqs():
    report = validate_config({8: 928000, 9: 902000})
    matches = [i for i in report.errors
               if i.sregs == (8, 9) and "MIN_FREQ" in i.title]
    assert matches, _titles(report)


# ------------------------------------------------------------ R3

def test_R3_too_many_channels():
    # 1000 channels across 26 MHz → 26 kHz/channel → too narrow.
    # Note: S10 here is out of S10's own valid range so R1 will also fire,
    # but R3 still triggers because it only inspects raw arithmetic.
    report = validate_config({8: 902000, 9: 928000, 10: 1000})
    spacing_warnings = [i for i in report.warnings
                        if "too narrow" in i.title.lower()]
    assert spacing_warnings, _titles(report)


# ------------------------------------------------------------ R4

def test_R4_rfd900p_allows_1w():
    # Every RFD900 variant is rated to 30 dBm — no model-cap error.
    report = validate_config({4: 30}, board_name="RFD900p")
    assert not any(i.sregs == (4,) and "RFD900p" in i.title for i in report.errors)


def test_R4_txpower_exceeds_model_cap(monkeypatch):
    from rfd import regions
    monkeypatch.setitem(regions.MODEL_MAX_TXPOWER, "RFD900p", 27)
    report = validate_config({4: 30}, board_name="RFD900p")
    r4 = [i for i in report.errors
          if i.sregs == (4,) and "RFD900p" in i.title]
    assert len(r4) == 1
    assert r4[0].suggested_value == 27


def test_R4_txpower_ok_on_RFD900x2():
    report = validate_config({4: 30}, board_name="RFD900x2")
    # No R4 error: 30 dBm is within the x2 ceiling.
    assert not any("exceeds" in i.title and "RFD900x2" in i.title
                   for i in report.errors)
    # R5 (info) still fires because S4 == 30.
    assert any(i.severity == "info" and "1 W output" in i.title
               for i in report.issues)


# ------------------------------------------------------------ R5

def test_R5_30dBm_info():
    report = validate_config({4: 30}, board_name="RFD900x2")
    r5 = [i for i in report.infos if "1 W output" in i.title]
    assert len(r5) == 1


# ------------------------------------------------------------ R7

def test_R7_skipped_on_remote():
    report = validate_config({2: 200}, board_name="RFD900p", is_remote=True)
    assert not any("AIR_SPEED 200" in i.title for i in report.issues)


def test_R7_fires_locally_on_8051_family():
    report = validate_config({2: 200}, board_name="RFD900p")
    assert any(i.severity == "warning"
               and i.sregs == (2,)
               and "AIR_SPEED 200" in i.title
               for i in report.issues)


# ------------------------------------------------------------ R8

def test_R8_unknown_band():
    report = validate_config({8: 700000, 9: 750000})
    r8 = [i for i in report.warnings
          if "doesn't match any known region" in i.title]
    assert r8
    assert report.detected_region is None


# ------------------------------------------------------------ R9

def test_R9_low_channel_count_US():
    report = validate_config({8: 902000, 9: 928000, 10: 10})
    r9 = [i for i in report.warnings
          if "below US minimum" in i.title and "(50)" in i.title]
    assert r9
    assert r9[0].suggested_value == 50


# ------------------------------------------------------------ R10

def test_R10_eu433_too_much_tx():
    report = validate_config({8: 433050, 9: 434790, 4: 20})
    r10 = [i for i in report.errors
           if "EU433 regulatory limit" in i.title and "(7 dBm)" in i.title]
    assert r10
    assert r10[0].suggested_value == 7


# ------------------------------------------------------------ R11

def test_R11_eu433_wrong_duty_cycle():
    report = validate_config({8: 433050, 9: 434790, 11: 100})
    r11 = [i for i in report.warnings
           if "DUTY_CYCLE=100" in i.title and "EU433" in i.title]
    assert r11
    assert r11[0].suggested_value == 10


# ------------------------------------------------------------ R12

def test_R12_eu433_missing_lbt():
    report = validate_config({8: 433050, 9: 434790, 12: 0})
    r12 = [i for i in report.warnings
           if "LBT_RSSI=0" in i.title and "EU433 minimum" in i.title]
    assert r12
    assert r12[0].suggested_value == 25


# ------------------------------------------------------------ R13

def test_R13_ecc_on():
    report = validate_config({5: 1})
    r13 = [i for i in report.warnings
           if "ECC enabled" in i.title and i.sregs == (5,)]
    assert r13
    assert r13[0].suggested_value == 0


# ------------------------------------------------------------ R14

def test_R14_mavlink_off_at_64kbps():
    report = validate_config({2: 64, 6: 0})
    assert any(i.severity == "info"
               and "MAVLink framing disabled" in i.title
               for i in report.issues)


# ------------------------------------------------------------ R15

def test_R15_mavlink_with_slow_link():
    report = validate_config({2: 8, 6: 1})
    assert any(i.severity == "warning"
               and "MAVLink framing on a slow link" in i.title
               for i in report.issues)


# ------------------------------------------------------------ R16

def test_R16_low_window_low_rate():
    report = validate_config({2: 8, 15: 33})
    assert any(i.severity == "warning"
               and "Low MAX_WINDOW" in i.title
               for i in report.issues)


# ------------------------------------------------------------ R17

def test_R17_rtscts_low_rate():
    report = validate_config({2: 8, 14: 1})
    assert any(i.severity == "info"
               and "RTS/CTS most useful" in i.title
               for i in report.issues)


# ------------------------------------------------------------ R18

def test_R18_default_netid():
    report = validate_config({3: 25})
    assert any(i.severity == "info"
               and "factory NETID" in i.title
               for i in report.issues)


# ------------------------------------------------------------ R19 firmware lockdown

_LOCKED_BANNER = "RFD SiK 3.57 on RFD900X2-US"


def test_R19_flags_lockdown_change_against_radio():
    """User wants S9=928000 but the radio's locked at 915000 → warning + suggested revert."""
    intended = {8: 902000, 9: 928000, 10: 50, 11: 100}
    current  = {8: 902000, 9: 915000, 10: 51, 11: 100}
    report = validate_config(
        intended,
        firmware_banner=_LOCKED_BANNER,
        current_values=current,
    )
    locked_warns = [i for i in report.warnings if "firmware-locked" in i.title]
    # Expect S9 and S10 (intended differ from current); S8 and S11 don't differ
    sregs = sorted({s for i in locked_warns for s in i.sregs})
    assert sregs == [9, 10]
    # The fix should suggest the radio's actual value
    s9_issue = next(i for i in locked_warns if 9 in i.sregs)
    assert s9_issue.suggested_value == 915000
    s10_issue = next(i for i in locked_warns if 10 in i.sregs)
    assert s10_issue.suggested_value == 51


def test_R19_silent_when_intended_matches_current():
    """If the user's value already matches what the radio has, no warning."""
    cfg = {8: 902000, 9: 915000, 10: 51, 2: 64, 15: 0}
    report = validate_config(
        cfg,
        firmware_banner=_LOCKED_BANNER,
        current_values=dict(cfg),
    )
    assert not any("firmware-locked" in i.title for i in report.issues)


def test_R19_skipped_for_unknown_banner():
    """No banner / non-matching banner → R19 doesn't fire."""
    intended = {9: 928000}
    current  = {9: 915000}
    # Empty banner
    rep = validate_config(intended, current_values=current)
    assert not any("firmware-locked" in i.title for i in rep.issues)
    # Banner that doesn't match any lockdown
    rep = validate_config(
        intended,
        firmware_banner="RFD SiK 1.7 on RFD900p",
        current_values=current,
    )
    assert not any("firmware-locked" in i.title for i in rep.issues)


def test_R19_skipped_on_remote_panel():
    """Remote validation skips R19 because the remote board isn't reliably known."""
    intended = {9: 928000}
    current  = {9: 915000}
    rep = validate_config(
        intended,
        firmware_banner=_LOCKED_BANNER,
        current_values=current,
        is_remote=True,
    )
    assert not any("firmware-locked" in i.title for i in rep.issues)


# ------------------------------------------------------------ integration

def test_valid_us_config_no_errors():
    cfg = _us_base()
    report = validate_config(cfg, board_name="RFD900x2")
    assert report.errors == []
    # detected_region must be the US envelope
    assert report.detected_region is not None
    assert report.detected_region.code == "US"
    # detected_board echoes back
    assert report.detected_board == "RFD900x2"


def test_valid_us_config_with_default_netid_still_clean_modulo_info():
    cfg = _us_base()
    cfg[3] = 25  # back to factory NETID → only an info
    cfg[4] = 30  # 1 W → only an info
    report = validate_config(cfg, board_name="RFD900x2")
    assert report.errors == []
    assert report.warnings == []
    titles = _titles(report)
    assert any("factory NETID" in t for t in titles)
    assert any("1 W output" in t for t in titles)
    # overall must collapse to "info"
    assert report.overall == "info"


def test_report_overall_severity_aggregates_correctly():
    issues = (
        ValidationIssue(severity="info", sregs=(3,), title="info-x", detail=""),
        ValidationIssue(severity="warning", sregs=(5,), title="warn-x", detail=""),
        ValidationIssue(severity="error", sregs=(2,), title="err-x", detail=""),
    )
    report = ValidationReport(
        issues=issues, detected_region=None, detected_board="")
    assert report.overall == "errors"

    warnings_only = ValidationReport(
        issues=(issues[0], issues[1]), detected_region=None, detected_board="")
    assert warnings_only.overall == "warnings"

    infos_only = ValidationReport(
        issues=(issues[0],), detected_region=None, detected_board="")
    assert infos_only.overall == "info"

    empty = ValidationReport(issues=(), detected_region=None, detected_board="")
    assert empty.overall == "ok"


def test_report_issues_by_sreg_indexes_correctly():
    # R2 yields a single issue with sregs=(8, 9); both keys must map to it.
    report = validate_config({8: 928000, 9: 902000})
    by_sreg = report.issues_by_sreg
    assert 8 in by_sreg and 9 in by_sreg
    matches_8 = [i for i in by_sreg[8] if "MIN_FREQ" in i.title]
    matches_9 = [i for i in by_sreg[9] if "MIN_FREQ" in i.title]
    assert matches_8 and matches_9
    # The same issue object is referenced under both keys.
    assert matches_8[0] is matches_9[0]


def test_validate_with_no_freqs_skips_regional_rules():
    # No S8/S9 → R8..R12 should never fire.
    report = validate_config({4: 20, 11: 100, 12: 0})
    forbidden_titles = [
        "doesn't match any known region",
        "below",                     # min_channels / lbt
        "regulatory limit",
        "doesn't match",             # duty cycle
    ]
    for issue in report.issues:
        for forbidden in forbidden_titles:
            # Allow benign R1/etc, but block the regional ones explicitly.
            if forbidden in issue.title:
                # The only way "below" could legitimately appear is in some
                # future non-regional rule; for now there isn't one.
                pytest.fail(f"unexpected regional issue: {issue.title!r}")
    assert report.detected_region is None


def test_eu433_region_matches_and_full_rule_chain():
    # Sanity: a deliberately bad EU433 config trips the expected rules.
    report = validate_config({
        4: 20, 8: 433050, 9: 434790, 10: 5, 11: 100, 12: 0,
    })
    assert report.detected_region is not None
    assert report.detected_region.code == "EU433"
    titles = _titles(report)
    assert any("EU433 regulatory limit" in t for t in titles)
    assert any("DUTY_CYCLE=100" in t and "EU433" in t for t in titles)
    assert any("EU433 minimum" in t for t in titles)


def test_is_remote_skips_model_specific_rules():
    # Even on a "weak" board, the remote-panel call must skip R4/R5/R7.
    report = validate_config(
        {2: 200, 4: 30}, board_name="RFD900p", is_remote=True)
    assert not any("RFD900p maximum" in i.title for i in report.issues)
    assert not any("AIR_SPEED 200" in i.title for i in report.issues)
    assert not any("1 W output" in i.title for i in report.issues)


def test_detected_region_us_for_us_range():
    report = validate_config({8: 902000, 9: 928000})
    assert report.detected_region is not None
    assert report.detected_region.code == "US"
    # Validate that the citation comes back unchanged from the regions module
    assert report.detected_region.citation == find_region("US").citation

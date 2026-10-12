"""Tests for the pure name-keyed config diff used by the Compare dialog."""
from __future__ import annotations

from rfd.diff import (
    SEVERITY_MISSING_A,
    SEVERITY_MISSING_B,
    SEVERITY_OK,
    SEVERITY_RF,
    SEVERITY_SOFT,
    SEVERITY_UART,
    diff_configs,
    severity_for,
    summarise,
)


# -------------------------------------------------- severity classification
def test_rf_critical_names():
    for n in ("NETID", "AIR_SPEED", "MIN_FREQ", "MAX_FREQ", "NUM_CHANNELS",
              "DUTY_CYCLE", "ECC", "MANCHESTER", "MAX_WINDOW", "LBT_RSSI",
              "ENCRYPTION_LEVEL"):
        assert severity_for(n) == SEVERITY_RF, n


def test_soft_names():
    for n in ("RTSCTS", "OPPRESEND", "MAVLINK", "AIR_FRAMELEN", "TXPOWER"):
        assert severity_for(n) == SEVERITY_SOFT, n


def test_uart_names_explicit_and_default():
    assert severity_for("SERIAL_SPEED") == SEVERITY_UART
    assert severity_for("FSFRAMELOSS") == SEVERITY_UART
    # Default for unknown
    assert severity_for("UNKNOWN_FUTURE_PARAM") == SEVERITY_UART


def test_gpio_names_classify_as_uart():
    assert severity_for("GPI1_1R/CIN") == SEVERITY_UART
    assert severity_for("GPO1_3STATLED") == SEVERITY_UART


# -------------------------------------------------- diff basics
def test_full_match_returns_all_ok():
    a = {"NETID": 41, "AIR_SPEED": 64, "MAX_WINDOW": 131}
    b = dict(a)
    entries = diff_configs(a, b)
    assert all(e.severity == SEVERITY_OK for e in entries)
    assert all(e.is_match for e in entries)


def test_rf_mismatch_promoted_to_top():
    a = {"NETID": 41, "AIR_SPEED": 64, "MAX_WINDOW": 131, "RTSCTS": 0}
    b = {"NETID": 42, "AIR_SPEED": 64, "MAX_WINDOW": 0, "RTSCTS": 1}
    entries = diff_configs(a, b)
    # First two entries should be RF-critical (NETID, MAX_WINDOW), then
    # soft (RTSCTS), then OK (AIR_SPEED).
    severities = [e.severity for e in entries]
    assert severities[0] == SEVERITY_RF
    assert severities[1] == SEVERITY_RF
    # The RF entries are alphabetically sorted within their tier.
    assert {entries[0].name, entries[1].name} == {"MAX_WINDOW", "NETID"}
    assert entries[2].name == "RTSCTS"
    assert entries[2].severity == SEVERITY_SOFT
    assert entries[-1].severity == SEVERITY_OK


def test_missing_in_b():
    a = {"NETID": 41, "MANCHESTER": 0}
    b = {"NETID": 41}
    entries = diff_configs(a, b)
    by_name = {e.name: e for e in entries}
    assert by_name["MANCHESTER"].severity == SEVERITY_MISSING_B
    assert by_name["MANCHESTER"].value_a == 0
    assert by_name["MANCHESTER"].value_b is None
    assert by_name["MANCHESTER"].is_missing


def test_missing_in_a():
    a = {"NETID": 41}
    b = {"NETID": 41, "ENCRYPTION_LEVEL": 0}
    entries = diff_configs(a, b)
    by_name = {e.name: e for e in entries}
    assert by_name["ENCRYPTION_LEVEL"].severity == SEVERITY_MISSING_A
    assert by_name["ENCRYPTION_LEVEL"].value_a is None
    assert by_name["ENCRYPTION_LEVEL"].value_b == 0


def test_sreg_mappings_propagate():
    a = {"NETID": 41, "MAX_WINDOW": 131}
    b = {"NETID": 41, "MAX_WINDOW": 131}
    a_sregs = {"NETID": 3, "MAX_WINDOW": 15}     # canonical SiK layout
    b_sregs = {"NETID": 3, "MAX_WINDOW": 14}     # RFDesign 3.x layout
    entries = diff_configs(a, b, a_sregs=a_sregs, b_sregs=b_sregs)
    by_name = {e.name: e for e in entries}
    assert by_name["MAX_WINDOW"].sreg_a == 15
    assert by_name["MAX_WINDOW"].sreg_b == 14
    # Different sregs but same value → still considered a match
    assert by_name["MAX_WINDOW"].is_match


def test_summarise_counts():
    entries = diff_configs(
        {"NETID": 41, "AIR_SPEED": 64, "RTSCTS": 0, "MAX_WINDOW": 131},
        {"NETID": 42, "AIR_SPEED": 64, "RTSCTS": 1, "MAX_WINDOW": 0},
    )
    counts = summarise(entries)
    assert counts.get(SEVERITY_RF, 0) == 2     # NETID, MAX_WINDOW
    assert counts.get(SEVERITY_SOFT, 0) == 1    # RTSCTS
    assert counts.get(SEVERITY_OK, 0) == 1      # AIR_SPEED


def test_real_world_user_scenario_max_window_diff():
    """Reproduces the bug we just diagnosed: same NETID + freq + air rate,
    but S14 MAX_WINDOW differs (0 vs 131).  Diff should rank that as the
    top concern."""
    a = {
        "NETID": 41, "AIR_SPEED": 64,
        "MIN_FREQ": 915000, "MAX_FREQ": 928000, "NUM_CHANNELS": 21,
        "DUTY_CYCLE": 100, "ECC": 0, "MAVLINK": 1,
        "RTSCTS": 0, "MAX_WINDOW": 131,
    }
    b = dict(a)
    b["MAX_WINDOW"] = 0
    b["RTSCTS"] = 1
    entries = diff_configs(a, b)
    top = next(e for e in entries if not e.is_match)
    assert top.name == "MAX_WINDOW"
    assert top.severity == SEVERITY_RF

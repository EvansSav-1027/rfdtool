from __future__ import annotations

import pytest

from rfd.regions import (
    MODEL_FAMILIES,
    MODEL_MAX_TXPOWER,
    REGIONS,
    Region,
    detect_region,
    find_region,
    model_family,
    model_max_txpower,
)


def test_regions_have_unique_codes():
    codes = [r.code for r in REGIONS]
    assert len(codes) == len(set(codes))


def test_regions_have_well_formed_freq_range():
    for r in REGIONS:
        assert r.min_freq < r.max_freq, r.code
        assert r.min_freq > 0


def test_find_region_known():
    r = find_region("US")
    assert r is not None and r.min_freq == 902000 and r.max_freq == 928000


def test_find_region_unknown():
    assert find_region("XX") is None


@pytest.mark.parametrize("s8,s9,expected", [
    (902000, 928000, "US"),                # exact US range
    (915001, 928000, "AU"),                # narrower → AU wins over US
    (921000, 928000, "NZ"),                # narrowest → NZ wins over AU/US
    (915000, 928000, "BR"),                # 915-928 inclusive starts at 915000 → BR (AU starts 915001)
    (433050, 434790, "EU433"),             # exact EU 433
    (433051, 434790, "AU433"),             # narrower → AU 433 wins over EU 433 / ZA 433
])
def test_detect_region_pattern_matches(s8, s9, expected):
    r = detect_region(s8, s9)
    assert r is not None and r.code == expected, f"got {r.code if r else None}"


def test_detect_region_outside_known_bands_returns_none():
    assert detect_region(700000, 750000) is None
    assert detect_region(2400000, 2480000) is None  # 2.4 GHz — not covered


def test_detect_region_invalid_input():
    assert detect_region(0, 0) is None
    assert detect_region(928000, 902000) is None  # inverted
    assert detect_region(-1, 928000) is None


def test_detect_region_too_wide():
    # User configured a range that spills outside any region
    assert detect_region(900000, 930000) is None


def test_us_region_constraints():
    r = find_region("US")
    assert r.min_channels == 50
    assert r.max_tx_dbm == 30


def test_eu433_region_constraints():
    r = find_region("EU433")
    assert r.duty_cycle == 10
    assert r.lbt_rssi_min == 25
    assert r.max_tx_dbm == 7


# -------------------------------------------------- model TX limits
@pytest.mark.parametrize("board,expected", [
    ("RFD900",      30),
    ("RFD900a",     30),
    ("RFD900u",     30),
    ("RFD900p",     30),
    ("RFD900x",     30),
    ("RFD900ux",    30),
    ("RFD900x2",    30),
])
def test_model_max_txpower_canonical(board, expected):
    assert model_max_txpower(board) == expected


def test_model_max_txpower_substring_match():
    # Real banner strings include the variant suffix
    assert model_max_txpower("RFD SiK 3.57 on RFD900X2-US") == 30
    assert model_max_txpower("RFD900P-AU") == 30


def test_model_max_txpower_prefers_specific_match():
    # "RFD900x2-US" contains both "RFD900" and "RFD900x2"; pick the longest
    assert model_max_txpower("RFD900x2-US") == 30


def test_model_max_txpower_unknown():
    assert model_max_txpower("Unknown") is None
    assert model_max_txpower("") is None


# -------------------------------------------------- model families
def test_every_known_board_belongs_to_a_family():
    all_boards = {b for fam in MODEL_FAMILIES for b in fam.boards}
    for board in MODEL_MAX_TXPOWER.keys():
        assert board in all_boards, f"{board} missing from MODEL_FAMILIES"


def test_model_family_lookup():
    assert model_family("RFD900x2").code == "stm32_v2"
    assert model_family("RFD900p").code == "8051"
    assert model_family("RFD900x").code == "stm32_v1"
    assert model_family("nope") is None


# -------------------------------------------------- firmware lockdowns
def test_firmware_lockdown_us_x2():
    from rfd.regions import firmware_lockdown
    locked = firmware_lockdown("RFD SiK 3.57 on RFD900X2-US")
    assert locked == frozenset({2, 8, 9, 10, 15})


def test_firmware_lockdown_us_x():
    from rfd.regions import firmware_lockdown
    assert firmware_lockdown("SiK 2.4 on RFD900X-US") == frozenset({2, 8, 9, 10, 15})


def test_firmware_lockdown_eu_variants():
    from rfd.regions import firmware_lockdown
    assert firmware_lockdown("RFD SiK 3.0 on RFD900X2-EU") == frozenset({2, 8, 9, 10, 15})
    assert firmware_lockdown("RFD SiK 2.7 on RFD900X-EU")  == frozenset({2, 8, 9, 10, 15})


def test_firmware_lockdown_unknown_banner_returns_empty():
    from rfd.regions import firmware_lockdown
    # Plain 8051 banner — no lockdown
    assert firmware_lockdown("RFD SiK 1.7 on RFD900p") == frozenset()
    # Empty
    assert firmware_lockdown("") == frozenset()


def test_lockdown_label():
    from rfd.regions import lockdown_label
    assert lockdown_label("RFD SiK 3.57 on RFD900X2-US") == "RFD900X2-US"
    assert lockdown_label("nothing") == ""

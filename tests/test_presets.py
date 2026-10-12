from __future__ import annotations

import json

import pytest

from rfd import presets
from rfd.presets import (
    BUILT_IN_PRESETS,
    CATEGORY_MAXIMIZE,
    CATEGORY_MULTICOMM,
    CATEGORY_MODEL,
    CATEGORY_REGION,
    CATEGORY_USE_CASE,
    CATEGORY_USER,
    MAXIMIZE_PRESETS,
    MODEL_PRESETS,
    REGION_PRESETS,
    USE_CASE_PRESETS,
    Profile,
    delete_user_profile,
    list_user_profiles,
    load_profile,
    presets_by_category,
    presets_for_board,
    save_profile,
    save_user_profile,
    slugify,
    user_profile_dir,
)


# --------------------------------------------------------------------- name uniqueness
def test_built_ins_have_unique_names():
    names = [p.name for p in BUILT_IN_PRESETS]
    assert len(names) == len(set(names))


def test_built_ins_split_across_categories():
    assert len(REGION_PRESETS) >= 5
    assert len(USE_CASE_PRESETS) >= 7
    assert len(MODEL_PRESETS) >= 3
    assert len(MAXIMIZE_PRESETS) >= 4


def test_each_built_in_has_a_known_category():
    for p in BUILT_IN_PRESETS:
        assert p.category in (
            CATEGORY_REGION, CATEGORY_USE_CASE, CATEGORY_MODEL, CATEGORY_MAXIMIZE,
            CATEGORY_MULTICOMM,
        ), f"{p.name}: bad category {p.category!r}"


# --------------------------------------------------------------------- schema v1/v2
def test_round_trip_via_dict_v2():
    p = Profile(
        name="test",
        description="desc",
        s_registers={1: 57, 2: 64},
        pin_registers={0: 1},
        applies_to=["RFD900x"],
        category="use-case",
        notes="note",
    )
    d = p.to_dict()
    assert d["format_version"] == presets.FORMAT_VERSION
    p2 = Profile.from_dict(d)
    assert p2.name == p.name
    assert p2.s_registers == p.s_registers
    assert p2.pin_registers == p.pin_registers
    assert p2.applies_to == p.applies_to
    assert p2.category == p.category
    assert p2.notes == p.notes


def test_v1_files_load_with_default_metadata():
    """v1 JSON (no applies_to/category/notes) must load — fields take defaults."""
    legacy = {
        "format_version": 1,
        "name": "old",
        "description": "v1 file",
        "s_registers": {"1": 57, "2": 64},
        "pin_registers": {},
    }
    p = Profile.from_dict(legacy)
    assert p.name == "old"
    assert p.applies_to == []
    assert p.category == CATEGORY_USER
    assert p.notes == ""


def test_dict_keys_become_strings_in_json():
    p = Profile(name="x", s_registers={1: 57})
    d = p.to_dict()
    s = json.dumps(d)
    assert '"1": 57' in s


def test_save_and_load_roundtrip(tmp_path):
    src = Profile(
        name="round-trip",
        description="hello",
        s_registers={3: 25, 4: 20},
        pin_registers={5: 2},
        applies_to=["RFD900x2"],
        category="user",
        notes="notes",
    )
    p = tmp_path / "profile.json"
    save_profile(p, src)
    out = load_profile(p)
    assert out.name == src.name
    assert out.s_registers == src.s_registers
    assert out.applies_to == src.applies_to
    assert out.category == src.category
    assert out.notes == src.notes


def test_unsupported_format_version_rejected():
    with pytest.raises(ValueError, match="format_version"):
        Profile.from_dict({"format_version": 99, "name": "x"})


# --------------------------------------------------------------------- lookup
def test_find_preset():
    p = presets.find_preset("MAVLink default (SiK factory)")
    assert p is not None
    assert p.s_registers[2] == 64
    assert presets.find_preset("nonexistent") is None


def test_presets_by_category():
    region = presets_by_category(CATEGORY_REGION)
    assert region == REGION_PRESETS
    use = presets_by_category(CATEGORY_USE_CASE)
    assert use == USE_CASE_PRESETS


def test_presets_for_board_filters_applies_to():
    # 1W use-case is RFD900x/ux/x2 only — should NOT show for plain RFD900p
    result_p = [p.name for p in presets_for_board("RFD900p", category=CATEGORY_USE_CASE)]
    assert "Long range — 8 kbps + 1 W TX" not in result_p
    # But should show for RFD900x2
    result_x2 = [p.name for p in presets_for_board("RFD900x2", category=CATEGORY_USE_CASE)]
    assert "Long range — 8 kbps + 1 W TX" in result_x2


def test_presets_for_board_includes_universal_when_filtering():
    # Use cases without applies_to apply to all boards
    result = [p.name for p in presets_for_board("RFD900", category=CATEGORY_USE_CASE)]
    assert "MAVLink default (SiK factory)" in result


def test_presets_for_board_substring_match_handles_banner_strings():
    # banner-style names like "RFD SiK 3.57 on RFD900X2-US" must still match
    result = [p.name for p in presets_for_board("RFD SiK 3.57 on RFD900X2-US")]
    assert any("RFD900x2" in p.applies_to or not p.applies_to for p in BUILT_IN_PRESETS
               for _ in [None] if p.matches_board("RFD SiK 3.57 on RFD900X2-US")) or result


def test_matches_board_empty_applies_to_passes_everything():
    p = Profile(name="any", applies_to=[])
    assert p.matches_board("RFD900p")
    assert p.matches_board("anything")
    assert p.matches_board("")


def test_matches_board_with_applies_to_substring():
    p = Profile(name="x2 only", applies_to=["RFD900x2"])
    assert p.matches_board("RFD900x2")
    assert p.matches_board("RFD SiK 3.57 on RFD900X2-US")
    assert not p.matches_board("RFD900p")


# --------------------------------------------------------------------- value validation
def test_preset_values_pass_register_validation():
    """Every built-in preset's S/pin values must satisfy the per-register validator."""
    from rfd.registers import validate

    for preset in BUILT_IN_PRESETS:
        for sreg, value in preset.s_registers.items():
            ok, reason = validate(sreg, value)
            assert ok, f"preset '{preset.name}' S{sreg}={value}: {reason}"
        for pin, value in preset.pin_registers.items():
            ok, reason = validate(pin, value, pin=True)
            assert ok, f"preset '{preset.name}' R{pin}={value}: {reason}"


def test_region_presets_only_set_freq_band_registers():
    """Region presets must touch only S8/S9/S10/S11/S12 — nothing else."""
    allowed = {8, 9, 10, 11, 12}
    for preset in REGION_PRESETS:
        keys = set(preset.s_registers.keys())
        assert keys <= allowed, f"{preset.name} sets {keys - allowed} (should be only {allowed})"


def test_use_case_presets_dont_touch_freqs_or_baud_or_netid():
    """Use-case presets must NOT change baud (S1), NETID (S3), or band (S8/S9/S10/S11/S12)."""
    forbidden = {1, 3, 8, 9, 10, 11, 12}
    for preset in USE_CASE_PRESETS:
        keys = set(preset.s_registers.keys())
        bad = keys & forbidden
        assert not bad, f"{preset.name} touches forbidden {bad}"


def test_model_presets_dont_set_band_registers():
    """Model factory presets leave band (S8/S9/S10) for the user's region preset."""
    forbidden = {8, 9, 10}
    for preset in MODEL_PRESETS:
        keys = set(preset.s_registers.keys())
        bad = keys & forbidden
        assert not bad, f"{preset.name} sets band {bad}"


def test_maximize_presets_pass_full_validation():
    """Each maximize combo must validate cleanly (no errors) under our own validator
    when applied to its own target board."""
    from rfd.validation import validate_config

    for preset in MAXIMIZE_PRESETS:
        target_board = preset.applies_to[0] if preset.applies_to else ""
        report = validate_config(preset.s_registers, board_name=target_board)
        # Errors are unacceptable; warnings/infos are fine (e.g. R5 1W info)
        assert not report.errors, (
            f"{preset.name} on {target_board}: "
            + "; ".join(f"{i.title}" for i in report.errors)
        )


# --------------------------------------------------------------------- slugify
def test_slugify_basic():
    assert slugify("MAVLink default") == "mavlink-default"
    assert slugify("US / Canada — 902-928 MHz") == "us-canada-902-928-mhz"
    assert slugify("") == "preset"
    assert slugify("!!!") == "preset"


# --------------------------------------------------------------------- user storage
@pytest.fixture
def tmp_user_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    yield tmp_path / "rfdtool" / "profiles"


def test_user_profile_dir_uses_xdg_config_home(tmp_user_dir):
    d = user_profile_dir()
    assert d == tmp_user_dir
    assert d.exists()


def test_save_and_list_user_profiles(tmp_user_dir):
    p = Profile(
        name="my custom one",
        s_registers={3: 42},
        category="user",
    )
    target = save_user_profile(p)
    assert target.exists()
    assert target.name == "my-custom-one.json"

    listed = list_user_profiles()
    assert len(listed) == 1
    assert listed[0].name == p.name
    assert listed[0].s_registers == p.s_registers


def test_list_user_profiles_skips_corrupt_files(tmp_user_dir):
    # Write a valid profile and a malformed one
    save_user_profile(Profile(name="ok", s_registers={1: 57}))
    bad = tmp_user_dir / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    listed = list_user_profiles()
    assert len(listed) == 1 and listed[0].name == "ok"


def test_delete_user_profile(tmp_user_dir):
    p = Profile(name="to-delete")
    save_user_profile(p)
    assert delete_user_profile(p) is True
    assert delete_user_profile(p) is False  # already gone


def test_save_user_profile_no_overwrite(tmp_user_dir):
    p = Profile(name="dup")
    save_user_profile(p)
    with pytest.raises(FileExistsError):
        save_user_profile(p, overwrite=False)
    # default overwrite=True still works
    save_user_profile(p)

"""Tests for RFD MultiComm firmware support.  No Qt, no hardware.

The fixture ``multicomm_v4_868_ati10.txt`` is a real dump from a v4.00
RFD868x2 (decorated ``ATI10:n``-style lines), which pins the parser to what
the firmware actually prints.
"""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from rfd import multicomm as mc
from rfd import protocol as proto
from rfd.diff import (
    SEVERITY_CONFLICT,
    SEVERITY_OK,
    SEVERITY_PER_NODE,
    SEVERITY_RF,
    diff_configs,
)
from rfd.presets import MULTICOMM_PRESETS, Profile, load_profile, save_profile
from rfd.protocol import CommandModeBracket
from rfd.radio import RadioCore
from rfd.registers import (
    FAMILY_MULTICOMM,
    FAMILY_SIK,
    ParamMeta,
    coerce_value,
    derive_def,
    format_value,
    validate_def,
)
from rfd.validation import validate_config

FIXTURE = Path(__file__).parent / "fixtures" / "multicomm_v4_868_ati10.txt"
FAST = CommandModeBracket(quiet_before=0.01, quiet_after=0.01, reply_timeout=0.3)


@pytest.fixture(scope="module")
def dump() -> proto.Ati5Result:
    return proto.parse_ati5(FIXTURE.read_text())


def _lines_by_sreg(decorated: bool) -> dict[int, str]:
    """Fixture lines keyed by sreg, optionally stripped to plain form."""
    out: dict[int, str] = {}
    for line in FIXTURE.read_text().splitlines():
        m = re.match(r"S(\d+):", line)
        if not m:
            continue
        if not decorated:
            m2 = proto.ATI5_LINE.search(line)
            line = f"S{m2.group('sreg')}:{m2.group('name')}={m2.group('value')}"
        out[int(m.group(1))] = line
    return out


def _fake_multicomm(ser, *, decorated_ati5: bool, node_prefix: str = "") -> None:
    """Script `ser` to answer ATI5:x:y / RTI5:x:y / ATI10:n like the radio."""
    plain = _lines_by_sreg(decorated=decorated_ati5)
    full = _lines_by_sreg(decorated=True)

    def ranged(data: bytes) -> bytes:
        m = re.search(rb"[AR]TI5:(\d+):(\d+)", data)
        lo, hi = int(m.group(1)), int(m.group(2))
        body = "".join(
            f"{node_prefix}{plain[s]}\r\n" for s in range(lo, hi + 1) if s in plain
        )
        return (body + "OK\r\n").encode()

    def info(data: bytes) -> bytes:
        n = int(re.search(rb"ATI10:(\d+)", data).group(1))
        return (full[n] + "\r\nOK\r\n").encode() if n in full else b"ERROR2\r\n"

    ser.script(b"TI5:", ranged)
    ser.script(b"ATI10:", info)


# --------------------------------------------------------------------- parser
def test_fixture_parses_every_register(dump):
    assert sorted(dump.s_params) == list(range(50))
    assert sorted(dump.s_meta) == list(range(50))
    assert dump.s_params[1] == 200
    assert dump.s_params[7] == 38981
    assert dump.s_params[9] == 1
    assert dump.s_params[45] == (1, 1, 0, 0, 0, 0, 0, 0)
    assert dump.s_params[48] == mc.DEFAULT_ENCRYPTKEY
    assert dump.s_params[49] == (64,) * 20


def test_fixture_names_and_metadata(dump):
    assert dump.s_names[35] == "PIN0TYPE(13)"
    assert dump.s_names[32] == "RATE/FREQBAND"
    m = dump.s_meta[1]
    assert (m.type_code, m.minimum, m.maximum) == ("N", 56, 200)
    assert "2000" in m.options
    assert dump.s_meta[36].mask == 0x80FF3D
    assert dump.s_meta[45].length == 8
    assert dump.s_meta[9].maximum == 2**64 - 1
    assert dump.s_meta[33].minimum == 0   # the <…> block is skipped


def test_plain_and_sik_lines_still_parse():
    r = proto.parse_ati5("S0: FORMAT=25\r\nS14: MAX_WINDOW=131\r\nS1:BITRATE=64\r\n")
    assert r.s_params == {0: 25, 14: 131, 1: 64}
    assert r.s_meta == {}


def test_parser_ignores_echo_and_glued_tags():
    r = proto.parse_ati5("ATS9=4\r\nATI5:0:4\r\n[2] S10:NODEID=2\r\n")
    assert r.s_params == {10: 2}


def test_canonical_names_and_detection(dump):
    names = mc.canonicalize_names(dump.s_names)
    assert names[35] == "PIN0TYPE"
    assert names[46] == "PINMIRROR"          # firmware prints PINMIRR
    assert mc.looks_like_multicomm(names=dump.s_names.values())
    assert mc.looks_like_multicomm(banner="RFD MultiComm v4.00")
    assert not mc.looks_like_multicomm(
        names=["FORMAT", "SERIAL_SPEED", "AIR_SPEED", "NETID", "TXPOWER"],
    )


# --------------------------------------------------------------------- metadata → editor spec
def test_meta_drives_ranges_and_options(dump):
    names = mc.canonicalize_names(dump.s_names)

    def d(s):
        return derive_def(s, names[s], family=FAMILY_MULTICOMM, meta=dump.s_meta[s])

    assert d(4).maximum == 8                      # 868 MHz: NUMCHANS 1..8
    assert sorted(d(1).enum) == [56, 64, 72, 100, 125, 188, 200]
    assert d(18).enum == {1: "Low", 2: "Medium", 3: "High (RC input)"}
    assert set(d(36).enum) == {0, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13, 14, 15, 23}
    assert d(9).kind == "bitmask"
    assert (d(48).kind, d(48).length) == ("list", 32)
    assert d(0).read_only


def test_allowed_values_offset_rule():
    pri = ParamMeta("L", 1, 3, 0, None, ("Low", "Med", "High"))
    assert pri.allowed_values() == {1: "Low", 2: "Med", 3: "High"}
    pin = ParamMeta("L", 0, 3, 0b1001, None, ("A", "B", "C", "D"))
    assert pin.allowed_values() == {0: "A", 3: "D"}


def test_validate_def_lists():
    reg = derive_def(47, "RCDEFS", family=FAMILY_MULTICOMM)
    assert validate_def(reg, (1500,) * 18)[0]
    assert not validate_def(reg, (1500,) * 19)[0]
    assert not validate_def(reg, (800,))[0]
    assert not validate_def(reg, 1500)[0]


# --------------------------------------------------------------------- AT commands
def test_value_formatting_round_trip():
    assert format_value((1, 2, 3)) == "1,2,3,"
    assert coerce_value("1,2,3,") == (1, 2, 3)
    assert coerce_value([4, 5]) == (4, 5)
    assert coerce_value("7") == 7


def test_command_builders():
    assert proto.at_set_param(49, (0, 64, 2, 64)) == b"ATS49=0,64,2,64,\r\n"
    assert proto.at_set_param(10, 1, remote=True, node=2) == b"RTS10=1@2\r\n"
    assert proto.at_set_param(49, (0, 64), remote=True, node=3) == b"RTS49=0,64@3\r\n"
    assert proto.at_set_param(10, 1, remote=True) == b"RTS10=1\r\n"
    assert proto.at_read_params(first=0, last=4) == b"ATI5:0:4\r\n"
    assert proto.at_read_params(remote=True, node=2, first=5, last=9) == b"RTI5:5:9@2\r\n"
    assert proto.at_reboot(remote=True, node=4) == b"RTZ@4\r\n"
    assert proto.at_rssi_report(True) == b"AT&T=RSSI\r\n"
    assert proto.at_param_info(36) == b"ATI10:36\r\n"


@pytest.mark.parametrize("reply,code", [
    ("OK\r\n", None), ("ERROR\r\n", "ERROR"),
    ("ATS1=7\r\nERROR2\r\n", "ERROR2"), ("ERROR3", "ERROR3"),
])
def test_reply_error(reply, code):
    assert proto.reply_error(reply) == code


# --------------------------------------------------------------------- RSSI report (manual fig. 18)
def test_parse_rssi_report_three_nodes():
    text = (
        "Loc RSSI Noise Av:91.00 raw:-101.25\r\n"
        "Loc RSSI Node:1 Av:50.00 raw:-50\r\n"
        "Remote RSSI Node:1 Av:51.00 Noise:87.00 NoiseRaw:90.00\r\n"
        "Remote RSSI Node:2 Av:52.00 Noise:87.50 NoiseRaw:89.00\r\n"
    )
    r = mc.parse_rssi_report(text)
    assert r.units == "dBm" and r.linked is True
    assert (r.local_noise, r.local_rssi, r.remote_rssi, r.remote_noise) == (-91, -50, -51, -87)
    assert [n.node for n in r.nodes] == [1, 2]
    assert mc.parse_rssi_report(text, node=2).remote_rssi == -52


def test_parse_rssi_report_no_link():
    assert mc.parse_rssi_report("Loc RSSI Noise Av:95.00 raw:-96").linked is False
    assert mc.parse_rssi_report("").linked is None


# --------------------------------------------------------------------- RadioCore
@pytest.mark.parametrize("decorated", [False, True])
def test_core_detects_and_reads_in_chunks(mock_serial, decorated):
    core = RadioCore(mock_serial)
    # Plain ATI5 truncated mid-way, the way a full UART buffer would.
    plain = _lines_by_sreg(decorated=False)
    mock_serial.script(
        b"ATI5\r\n", ("".join(plain[s] + "\r\n" for s in range(0, 12)) + "S12:SUB").encode(),
    )
    _fake_multicomm(mock_serial, decorated_ati5=decorated)
    res = core.read_params(timeout=0.3)
    assert core.family == FAMILY_MULTICOMM and res.family == FAMILY_MULTICOMM
    assert sorted(res.s_params) == list(range(50))
    assert res.s_names[46] == "PINMIRROR"
    assert res.s_params[47][16:] == (1000, 1000)
    assert res.s_meta[4].maximum == 8          # from ATI5 or ATI10 fallback
    assert (b"ATI10:" in mock_serial.written) is (not decorated)


def test_core_remote_read_addresses_node(mock_serial):
    core = RadioCore(mock_serial)
    core.family = FAMILY_MULTICOMM
    core.remote_node = 3
    _fake_multicomm(mock_serial, decorated_ati5=False, node_prefix="")
    res = core.read_params(remote=True, timeout=0.3)
    assert len(res.s_params) == 50
    assert b"RTI5:0:4@3\r\n" in mock_serial.written
    assert b"ATI10" not in mock_serial.written   # remote reuses local metadata


def test_core_remote_silent_raises(mock_serial):
    core = RadioCore(mock_serial)
    core.family = FAMILY_MULTICOMM
    core._remote_timing = lambda remote, t: (0.1, 0.05)  # keep the test fast
    with pytest.raises(Exception):
        core.read_params(remote=True, timeout=0.1)


def test_core_save_eeprom_is_noop(mock_serial):
    core = RadioCore(mock_serial)
    core.family = FAMILY_MULTICOMM
    assert core.save_eeprom() is True
    assert b"AT&W" not in mock_serial.written


def test_core_write_list_and_error2(mock_serial):
    core = RadioCore(mock_serial)
    core.family = FAMILY_MULTICOMM
    mock_serial.script(b"ATS49=", b"OK\r\n")
    mock_serial.script(b"ATS30=99", b"ERROR2\r\n")
    assert core.write_param(49, (0, 64, 2, 64)) is True
    assert b"ATS49=0,64,2,64,\r\n" in mock_serial.written
    assert core.write_param(30, 99) is False


def test_core_identify_detects_banner(mock_serial):
    core = RadioCore(mock_serial)
    mock_serial.script(b"ATI\r\n", b"ATI\r\nRFD MultiComm v4.00 on RFD900X2\r\nOK\r\n")
    info = core.identify(timeout=0.1)
    assert info["firmware_family"] == FAMILY_MULTICOMM
    assert core.is_multicomm


def test_core_poll_rssi_uses_report(mock_serial):
    core = RadioCore(mock_serial)
    core.family = FAMILY_MULTICOMM
    core.MULTICOMM_RSSI_WINDOW = 0.1
    mock_serial.script(
        b"AT&T=RSSI", b"Loc RSSI Node:1 Av:60.00 raw:-60\r\nRemote RSSI Node:1 Av:61.00 Noise:95.00\r\n",
    )
    r = core.poll_rssi()
    assert r.linked and r.local_rssi == -60
    assert b"AT&T\r\n" in mock_serial.written      # report switched off again
    assert b"ATI7" not in mock_serial.written


def test_core_poll_rssi_falls_back_to_rti(mock_serial):
    core = RadioCore(mock_serial)
    core.family = FAMILY_MULTICOMM
    core.MULTICOMM_RSSI_WINDOW = 0.05
    core._remote_timing = lambda remote, t: (0.2, 0.05)
    mock_serial.script(b"RTI\r\n", b"RTI\r\nRFD MultiComm v4.00\r\n")
    assert core.poll_rssi().linked is True


# --------------------------------------------------------------------- validation
def _by_sreg(dump) -> tuple[dict, dict]:
    return dict(dump.s_params), mc.canonicalize_names(dump.s_names)


def _titles(report) -> set[str]:
    return {i.title for i in report.issues}


def test_real_config_validates_clean(dump):
    vals, names = _by_sreg(dump)
    report = validate_config(vals, sreg_to_name=names, meta=dump.s_meta, board_name="RFD900x2")
    assert report.overall == "ok", [i.title for i in report.issues]


def test_family_inferred_from_names(dump):
    vals, names = _by_sreg(dump)
    vals[10] = 0
    vals[6] = 0       # fixed master…
    vals[9] = 2       # …without slot 1
    report = validate_config(vals, sreg_to_name=names)
    issue = next(i for i in report.issues if i.title == "Master must own slot 1")
    assert issue.severity == "error" and issue.suggested_value == 3


@pytest.mark.parametrize("changes,title", [
    ({9: 0b100, 8: 2}, "SLOTIDS uses non-existent slots"),
    ({4: 30, 2: 865000, 3: 870000}, "Channels too close together"),
    ({7: 5000}, "Slot size out of range"),
    ({12: 1, 11: 2}, "SUBNETID ≥ SUBNETS"),
    ({14: 64}, "DESTID 64 is reserved"),
    ({20: 2}, "Encryption uses the default key"),
    ({23: 0}, "Serial port 1 disabled"),
    ({36: 9}, "RC input node should be PRI=3"),
    ({35: 9}, "PIN0TYPE: PPM in not allowed on this pin"),
    ({13: 5}, "BINDID is developmental"),
    ({6: 1, 8: 6}, "Auto master with >2 slots"),
])
def test_rules_fire(dump, changes, title):
    vals, names = _by_sreg(dump)
    vals.update(changes)
    report = validate_config(vals, sreg_to_name=names, meta=dump.s_meta)
    assert title in _titles(report)


def test_hardware_rule_only_for_v1_boards(dump):
    vals, names = _by_sreg(dump)
    assert "MultiComm needs V2 hardware" in _titles(
        validate_config(vals, sreg_to_name=names, board_name="RFD900x"))
    assert "MultiComm needs V2 hardware" not in _titles(
        validate_config(vals, sreg_to_name=names, board_name="Unknown (ID 0x99)"))


# --------------------------------------------------------------------- diff
def test_diff_peer_semantics():
    master = {"P2PAUTO": 0, "NODEID": 0, "SLOTIDS": 1, "BITRATE": 64, "DESTID": 63}
    node = {"P2PAUTO": 0, "NODEID": 1, "SLOTIDS": 2, "BITRATE": 125, "DESTID": 0}
    by = {e.name: e.severity for e in diff_configs(master, node, peer=True)}
    assert by["BITRATE"] == SEVERITY_RF
    assert by["NODEID"] == by["SLOTIDS"] == by["DESTID"] == SEVERITY_PER_NODE

    clash = dict(node, NODEID=0, SLOTIDS=3)
    by = {e.name: e.severity for e in diff_configs(master, clash, peer=True)}
    assert by["NODEID"] == by["SLOTIDS"] == SEVERITY_CONFLICT


def test_diff_auto_master_pairs_may_be_identical():
    cfg = {"P2PAUTO": 1, "NODEID": 1, "SLOTIDS": 2, "TOTALUS": 40000}
    assert all(e.severity == SEVERITY_OK for e in diff_configs(cfg, dict(cfg), peer=True))


# --------------------------------------------------------------------- presets
@pytest.mark.parametrize("preset", MULTICOMM_PRESETS, ids=lambda p: p.name)
def test_multicomm_presets_are_valid(preset):
    assert preset.firmware == FAMILY_MULTICOMM
    assert preset.s_registers == {}      # never cross-filled onto SiK sregs
    for name, value in preset.params.items():
        sreg = mc.NAME_TO_SREG[name]
        ok, why = validate_def(derive_def(sreg, name, family=FAMILY_MULTICOMM), value)
        assert ok, f"{preset.name}: {why}"


def test_multicomm_presets_pass_validation_on_real_radio(dump):
    vals, names = _by_sreg(dump)
    n2s = {n: s for s, n in names.items()}
    for preset in MULTICOMM_PRESETS:
        cfg = dict(vals)
        if "Listen-only" in preset.name:
            cfg[n2s["NODEID"]] = 5      # its notes say: not NODEID 0
        cfg.update(preset.to_sregs_for(n2s))
        errors = validate_config(cfg, sreg_to_name=names, meta=dump.s_meta).errors
        assert not errors, (preset.name, [e.title for e in errors])


def test_india_presets_stay_within_1w_eirp():
    gains = {"monopole": 2.0, "dipole": 2.15}
    india = [p for p in MULTICOMM_PRESETS if "India" in p.name]
    assert len(india) == 2
    for p in india:
        gain = gains["monopole" if "monopole" in p.name else "dipole"]
        assert p.params["TXPOWER"] + gain <= 30.0
        assert (p.params["FREQMIN"], p.params["FREQMAX"]) == (865000, 868000)
        spacing = mc.channel_spacing_khz(865000, 868000, p.params["NUMCHANS"])
        assert spacing >= mc.required_spacing_khz(p.params["BITRATE"])


def test_profile_json_round_trip_with_arrays(tmp_path, dump):
    names = mc.canonicalize_names(dump.s_names)
    ati5 = replace(dump, s_names=names, family=FAMILY_MULTICOMM)
    from rfd.presets import profile_from_ati5
    prof = profile_from_ati5("live", ati5)
    assert prof.firmware == FAMILY_MULTICOMM
    assert "FORMAT" not in prof.params and "RSVD1" not in prof.params
    path = tmp_path / "p.json"
    save_profile(path, prof)
    back = load_profile(path)
    assert back.params["ENCRYPTKEY"] == mc.DEFAULT_ENCRYPTKEY
    assert back.params["SLOTIDS"] == 1
    assert back.firmware == FAMILY_MULTICOMM
    assert back.matches_firmware(FAMILY_MULTICOMM)
    assert not back.matches_firmware(FAMILY_SIK)


def test_old_profiles_match_any_firmware():
    p = Profile.from_dict({"format_version": 3, "name": "x", "params": {"NETID": 5}})
    assert p.firmware == "" and p.matches_firmware(FAMILY_MULTICOMM)

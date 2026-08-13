"""GDL90 encoder tests.

Known-answer values come from the GARMIN GDL 90 Data Interface
Specification (560-1058-00 Rev A) examples.

Copyright Sensors & Signals LLC https://www.snstac.com/
SPDX-License-Identifier: Apache-2.0
"""

import pytest

from gdlcot import gdl90

# --- CRC ---------------------------------------------------------------


def test_crc_known_answer_spec_heartbeat_example():
    """Spec §2.2.3: heartbeat 00 81 41 DB D0 08 02 has CRC bytes B3 8B."""
    message = bytes.fromhex("008141DBD00802")
    crc = gdl90.crc16(message)
    assert crc & 0xFF == 0xB3  # low byte, sent first
    assert (crc >> 8) & 0xFF == 0x8B  # high byte, sent second


def test_crc_empty():
    assert gdl90.crc16(b"") == 0


def test_frame_spec_heartbeat_example():
    """Full framed heartbeat from the spec: 7E 00 81 41 DB D0 08 02 B3 8B 7E."""
    message = bytes.fromhex("008141DBD00802")
    assert gdl90.frame(message) == bytes.fromhex("7E008141DBD00802B38B7E")


# --- Byte stuffing / framing round-trip ---------------------------------


def test_byte_stuff_escapes_flag_and_escape():
    assert gdl90.byte_stuff(b"\x7e") == b"\x7d\x5e"
    assert gdl90.byte_stuff(b"\x7d") == b"\x7d\x5d"
    assert gdl90.byte_stuff(b"\x01\x7e\x02\x7d\x03") == b"\x01\x7d\x5e\x02\x7d\x5d\x03"


def test_byte_stuff_round_trip():
    data = bytes(range(256)) + b"\x7e\x7d\x7e\x7e\x7d\x7d"
    assert gdl90.byte_unstuff(gdl90.byte_stuff(data)) == data


def test_frame_deframe_round_trip():
    message = bytes([0x14, 0x7E, 0x7D]) + bytes(range(25))
    framed = gdl90.frame(message)
    assert framed[0] == 0x7E and framed[-1] == 0x7E
    # No unescaped flag bytes inside the frame:
    assert 0x7E not in framed[1:-1]
    assert gdl90.deframe(framed) == message


def test_deframe_rejects_bad_crc():
    framed = bytearray(gdl90.frame(b"\x00\x81\x41\xdb\xd0\x08\x02"))
    framed[2] ^= 0x01  # corrupt payload
    assert gdl90.deframe(bytes(framed)) is None


# --- Lat/Lon semicircles -------------------------------------------------


def test_latlon_positive_45():
    assert gdl90.encode_latlon(45.0) == 0x200000


def test_latlon_negative_45_twos_complement():
    assert gdl90.encode_latlon(-45.0) == 0xE00000


def test_latlon_zero_and_extremes():
    assert gdl90.encode_latlon(0.0) == 0x000000
    assert gdl90.encode_latlon(-90.0) == 0xC00000
    assert gdl90.encode_latlon(90.0) == 0x400000
    assert gdl90.encode_latlon(-180.0) == 0x800000
    # +180 deg would be 2^23, one past the max positive value: clamped.
    assert gdl90.encode_latlon(180.0) == 0x7FFFFF


# --- Field packing edge cases --------------------------------------------


def test_altitude_encoding():
    assert gdl90.encode_altitude(None) == 0xFFF  # invalid marker
    assert gdl90.encode_altitude(0) == 40  # (0 + 1000) / 25
    assert gdl90.encode_altitude(5000) == 240
    assert gdl90.encode_altitude(-1000) == 0
    assert gdl90.encode_altitude(-5000) == 0  # clamped at field minimum
    assert gdl90.encode_altitude(1e9) == 0xFFE  # clamped below invalid


def test_hvelocity_encoding():
    assert gdl90.encode_hvelocity(None) == 0xFFF
    assert gdl90.encode_hvelocity(0) == 0
    assert gdl90.encode_hvelocity(123) == 123
    assert gdl90.encode_hvelocity(99999) == 0xFFE  # clamped below invalid


def test_vvelocity_encoding():
    assert gdl90.encode_vvelocity(None) == 0x800  # invalid marker
    assert gdl90.encode_vvelocity(0) == 0
    assert gdl90.encode_vvelocity(128) == 2  # 64 fpm units
    assert gdl90.encode_vvelocity(-64) == 0xFFF  # -1 in 12-bit two's complement
    assert gdl90.encode_vvelocity(1e9) != 0x800  # clamp never hits invalid
    assert gdl90.encode_vvelocity(-1e9) != 0x800


def test_track_encoding():
    assert gdl90.encode_track(0.0) == 0
    assert gdl90.encode_track(90.0) == 64
    assert gdl90.encode_track(180.0) == 128
    assert gdl90.encode_track(360.0) == 0
    assert gdl90.encode_track(-90.0) == 192


# --- Heartbeat ------------------------------------------------------------


def test_heartbeat_matches_spec_example():
    """Reconstruct the spec heartbeat: st1=81 st2=41 ts=D0DB counts 08 02."""
    message = gdl90.heartbeat(
        0xD0DB, status1=0x81, status2=0x41, uplink_count=1, basic_long_count=2
    )
    assert message == bytes.fromhex("008141DBD00802")


def test_heartbeat_timestamp_bit16_in_status2():
    message = gdl90.heartbeat(0x1D0DB, status1=0x81, status2=0x41)
    assert message[2] & 0x80  # ts bit 16 -> status2 bit 7
    assert message[3] == 0xDB  # low byte first (little-endian)
    assert message[4] == 0xD0


def test_heartbeat_defaults():
    message = gdl90.heartbeat(0)
    assert len(message) == 7
    assert message[0] == 0x00
    assert message[1] == 0x81  # UAT initialized + UTC ok
    assert message[2:] == bytes(5)


# --- Traffic / Ownship report ---------------------------------------------


GOLDEN_KWARGS = dict(
    alert=0,
    addr_type=0,
    address=0xABC123,
    alt_ft=5000.0,
    misc=gdl90.MISC_AIRBORNE_TRUE_TRACK,
    nic=8,
    nacp=8,
    hvel_kt=120.0,
    vvel_fpm=128.0,
    track_deg=90.0,
    emitter=gdl90.EMITTER_LIGHT,
    callsign="N123AB",
    priority=0,
)

# Hand-computed 28-byte golden report for the input above:
#   id=14, st=00, addr=AB C1 23,
#   lat +45 deg  -> 20 00 00, lon -45 deg -> E0 00 00,
#   alt (5000+1000)/25 = 240 = 0x0F0, misc 0x9 -> 0F 09,
#   nic/nacp 8/8 -> 88,
#   hvel 120 kt = 0x078, vvel +128 fpm = +2 -> 07 80 02,
#   track 90 deg -> 0x40, emitter 01,
#   callsign "N123AB  " -> 4E 31 32 33 41 42 20 20, priority 00.
GOLDEN_TRAFFIC = bytes.fromhex(
    "14 00 ABC123 200000 E00000 0F09 88 078002 40 01"
    " 4E3132334142 2020 00".replace(" ", "")
)


def test_traffic_report_golden_bytes():
    message = gdl90.traffic_report(45.0, -45.0, **GOLDEN_KWARGS)
    assert len(message) == 28
    assert message == GOLDEN_TRAFFIC


def test_ownship_report_differs_only_in_msg_id():
    traffic = gdl90.traffic_report(45.0, -45.0, **GOLDEN_KWARGS)
    ownship = gdl90.ownship_report(45.0, -45.0, **GOLDEN_KWARGS)
    assert ownship[0] == 0x0A
    assert ownship[1:] == traffic[1:]


def test_traffic_report_invalid_markers():
    message = gdl90.traffic_report(0.0, 0.0)
    # altitude 0xFFF invalid + misc:
    assert message[11] == 0xFF
    assert message[12] >> 4 == 0xF
    # hvel 0xFFF invalid, vvel 0x800 invalid:
    assert message[14] == 0xFF
    assert message[15] == 0xF8
    assert message[16] == 0x00


def test_traffic_report_callsign_padding_and_truncation():
    message = gdl90.traffic_report(0.0, 0.0, callsign="A")
    assert message[19:27] == b"A       "
    message = gdl90.traffic_report(0.0, 0.0, callsign="ABCDEFGHIJ")
    assert message[19:27] == b"ABCDEFGH"


def test_alert_and_priority_nibbles():
    message = gdl90.traffic_report(0.0, 0.0, alert=1, addr_type=2, priority=3)
    assert message[1] == 0x12
    assert message[27] == 0x30


# --- Ownship Geometric Altitude --------------------------------------------


def test_ownship_geo_altitude():
    message = gdl90.ownship_geo_altitude(1000.0)
    assert message == bytes((0x0B, 0x00, 0xC8, 0x7F, 0xFF))  # 1000/5 = 200


def test_ownship_geo_altitude_negative():
    message = gdl90.ownship_geo_altitude(-100.0)  # -20 -> 0xFFEC
    assert message[:3] == bytes((0x0B, 0xFF, 0xEC))


# --- Emitter category mapping -----------------------------------------------


@pytest.mark.parametrize(
    "cot_type,expected",
    [
        ("a-f-A-C-F", gdl90.EMITTER_LIGHT),
        ("a-f-A", gdl90.EMITTER_LIGHT),
        ("a-f-A-C-H", gdl90.EMITTER_ROTORCRAFT),
        ("a-h-A-M-H", gdl90.EMITTER_ROTORCRAFT),
        ("a-f-A-M-F-Q", gdl90.EMITTER_UAV),
        ("a-n-A-C-L", gdl90.EMITTER_LIGHTER_THAN_AIR),
        ("a-f-A-C-G", gdl90.EMITTER_GLIDER),
        ("", gdl90.EMITTER_LIGHT),
    ],
)
def test_emitter_category(cot_type, expected):
    assert gdl90.emitter_category(cot_type) == expected

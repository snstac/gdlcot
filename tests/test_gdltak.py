"""CoT parsing, track table and GDL90 beacon tests.

Copyright Sensors & Signals LLC https://www.snstac.com/
SPDX-License-Identifier: Apache-2.0
"""

import socket

import pytest

from gdltak import gdl90
import gdltak.gdltak as gdltak


# adsbcot-style CoT event:
ADSB_COT = b"""<event version="2.0" uid="ICAO-A1B2C3" type="a-f-A-C-F"
  time="2026-07-15T00:00:00.000000Z" start="2026-07-15T00:00:00.000000Z"
  stale="2026-07-15T00:02:00.000000Z" how="m-g">
  <point lat="37.76" lon="-122.4" hae="1524.0" ce="9999999.0" le="9999999.0"/>
  <detail>
    <contact callsign="N123AB"/>
    <track course="90.0" speed="61.7"/>
  </detail>
</event>"""

HELO_COT = b"""<event version="2.0" uid="ICAO-DEADBF" type="a-f-A-C-H"
  time="2026-07-15T00:00:00Z" start="2026-07-15T00:00:00Z"
  stale="2026-07-15T00:02:00Z" how="m-g">
  <point lat="37.0" lon="-122.0" hae="300.0" ce="10.0" le="10.0"/>
  <detail><contact callsign="MEDIC1"/></detail>
</event>"""

GROUND_COT = b"""<event version="2.0" uid="GPSTAK-host" type="a-f-G"
  time="2026-07-15T00:00:00Z" start="2026-07-15T00:00:00Z"
  stale="2026-07-15T00:00:10Z" how="m-g">
  <point lat="37.5" lon="-122.5" hae="10.0" ce="5.0" le="5.0"/>
  <detail><contact callsign="BASE"/></detail>
</event>"""


# --- CoT parsing ------------------------------------------------------------


def test_parse_cot_adsbcot_event():
    track = gdltak.parse_cot(ADSB_COT)
    assert track is not None
    assert track["uid"] == "ICAO-A1B2C3"
    assert track["cot_type"] == "a-f-A-C-F"
    assert track["lat"] == pytest.approx(37.76)
    assert track["lon"] == pytest.approx(-122.4)
    assert track["alt_ft"] == pytest.approx(5000.0, abs=0.1)  # 1524 m
    assert track["course"] == pytest.approx(90.0)
    assert track["speed_kt"] == pytest.approx(119.9, abs=0.1)  # 61.7 m/s
    assert track["callsign"] == "N123AB"
    assert track["address"] == 0xA1B2C3
    assert track["addr_type"] == gdltak.ADDR_TYPE_ICAO


def test_parse_cot_non_icao_uid_gets_self_assigned_address():
    track = gdltak.parse_cot(GROUND_COT)
    assert track["addr_type"] == gdltak.ADDR_TYPE_SELF_ASSIGNED
    assert 0 <= track["address"] <= 0xFFFFFF
    # Deterministic across parses:
    assert track["address"] == gdltak.parse_cot(GROUND_COT)["address"]


def test_parse_cot_null_sentinels_become_none():
    cot = ADSB_COT.replace(b'hae="1524.0"', b'hae="9999999.0"')
    assert gdltak.parse_cot(cot)["alt_ft"] is None


def test_parse_cot_rejects_garbage_and_non_events():
    assert gdltak.parse_cot(b"not xml") is None
    assert gdltak.parse_cot(b"<foo/>") is None
    assert gdltak.parse_cot(b'<event uid="x"/>') is None  # no point
    assert gdltak.parse_cot(None) is None
    assert gdltak.parse_cot(12345) is None  # e.g. takproto object


def test_is_air_track():
    assert gdltak.is_air_track("a-f-A-C-F")
    assert gdltak.is_air_track("a-h-A")
    assert not gdltak.is_air_track("a-f-G")
    assert not gdltak.is_air_track("b-m-p-s-m")
    assert not gdltak.is_air_track("")


# --- Track table -------------------------------------------------------------


def test_track_table_update_and_expiry():
    table = gdltak.TrackTable(stale_secs=60)
    assert table.update_from_cot(ADSB_COT, now=0.0)
    assert table.update_from_cot(HELO_COT, now=10.0)
    assert len(table.fresh(now=30.0)) == 2

    # At t=61 the first track (t=0) is stale, the second (t=10) is fresh:
    fresh = table.fresh(now=61.0)
    assert [t["uid"] for t in fresh] == ["ICAO-DEADBF"]

    # At t=71 everything is stale:
    assert table.fresh(now=71.0) == []


def test_track_table_refresh_resets_expiry():
    table = gdltak.TrackTable(stale_secs=60)
    table.update_from_cot(ADSB_COT, now=0.0)
    table.update_from_cot(ADSB_COT, now=50.0)
    assert len(table.fresh(now=100.0)) == 1


def test_track_table_filters_non_air_tracks():
    table = gdltak.TrackTable(stale_secs=60)
    assert not table.update_from_cot(GROUND_COT, now=0.0)
    assert table.fresh(now=0.0) == []


def test_track_table_accepts_ground_ownship():
    table = gdltak.TrackTable(stale_secs=60, ownship_uid="GPSTAK-host")
    assert table.update_from_cot(GROUND_COT, now=0.0)
    assert table.get("GPSTAK-host")["callsign"] == "BASE"


# --- Track -> GDL90 report ----------------------------------------------------


def test_track_to_report_fields():
    track = gdltak.parse_cot(ADSB_COT)
    message = gdltak.track_to_report(track)
    assert message[0] == gdl90.MSG_TRAFFIC
    assert message[2:5] == bytes((0xA1, 0xB2, 0xC3))
    assert message[19:27] == b"N123AB  "
    assert message[17] == 64  # track 90 deg
    # Helicopter emitter category:
    helo = gdltak.track_to_report(gdltak.parse_cot(HELO_COT))
    assert helo[18] == gdl90.EMITTER_ROTORCRAFT


# --- Beacon / egress -----------------------------------------------------------


class FakeSender:
    def __init__(self):
        self.messages = []
        self.addr = ("127.0.0.1", 4000)

    def send(self, message):
        self.messages.append(message)


def make_worker(sender, tracks, **cfg):
    config = {
        "COT_URL": "udp+ro://239.2.3.1:6969",
        "UPDATE_HZ": "1",
        "CALLSIGN": "GDLTAK",
        **cfg,
    }
    return gdltak.Gdl90Worker(None, config, tracks, sender)


def test_beacon_heartbeat_and_traffic_only_without_ownship():
    tracks = gdltak.TrackTable(stale_secs=60)
    tracks.update_from_cot(ADSB_COT)
    sender = FakeSender()
    worker = make_worker(sender, tracks)
    assert worker.beacon_once(now_wall=3600.0) == 2
    assert sender.messages[0][0] == gdl90.MSG_HEARTBEAT
    # Heartbeat timestamp = seconds since UTC midnight:
    assert sender.messages[0][3] | (sender.messages[0][4] << 8) == 3600
    assert sender.messages[1][0] == gdl90.MSG_TRAFFIC


def test_beacon_static_ownship():
    tracks = gdltak.TrackTable(stale_secs=60)
    sender = FakeSender()
    worker = make_worker(
        sender,
        tracks,
        OWNSHIP_LAT="37.76",
        OWNSHIP_LON="-122.4",
        OWNSHIP_ALT_FT="100",
    )
    worker.beacon_once()
    ids = [m[0] for m in sender.messages]
    assert ids == [
        gdl90.MSG_HEARTBEAT,
        gdl90.MSG_OWNSHIP,
        gdl90.MSG_OWNSHIP_GEO_ALT,
    ]
    assert b"GDLTAK" in sender.messages[1]


def test_beacon_cot_ownship_excluded_from_traffic():
    tracks = gdltak.TrackTable(stale_secs=60, ownship_uid="GPSTAK-host")
    tracks.update_from_cot(GROUND_COT)
    tracks.update_from_cot(ADSB_COT)
    sender = FakeSender()
    worker = make_worker(sender, tracks, OWNSHIP_UID="GPSTAK-host")
    worker.beacon_once()
    ids = [m[0] for m in sender.messages]
    assert ids.count(gdl90.MSG_OWNSHIP) == 1
    assert ids.count(gdl90.MSG_TRAFFIC) == 1  # ownship not repeated as traffic
    ownship = sender.messages[ids.index(gdl90.MSG_OWNSHIP)]
    assert b"BASE" in ownship


def test_gdl90_sender_unicast_datagram_is_framed():
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(2)
    port = receiver.getsockname()[1]

    sender = gdltak.Gdl90Sender(f"udp://127.0.0.1:{port}")
    heartbeat = gdl90.heartbeat(0)
    sender.send(heartbeat)
    datagram = receiver.recv(1024)
    receiver.close()

    assert datagram[0] == 0x7E and datagram[-1] == 0x7E
    assert gdl90.deframe(datagram) == heartbeat


def test_gdl90_sender_broadcast_scheme():
    sender = gdltak.Gdl90Sender("udp+broadcast://255.255.255.255:4000")
    assert sender.addr == ("255.255.255.255", 4000)
    assert sender.sock.getsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST)


def test_gdl90_sender_rejects_non_udp():
    with pytest.raises(ValueError):
        gdltak.Gdl90Sender("tcp://127.0.0.1:4000")


# --- Pressure vs geometric altitude ---------------------------------------
#
# GDL90 Traffic Reports carry PRESSURE altitude; the Ownship Geometric Altitude
# message carries geometric. CoT's point/@hae is geometric, so using it for
# traffic mixes datums. adsbcot publishes readsb's alt_baro (already in feet)
# as <__adsb alt_baro="...">, which is the right source.

# 1524.0 m HAE == 5000 ft geometric. alt_baro says 4800 ft pressure -- a 200 ft
# disagreement, which is an ordinary geometric-vs-pressure difference and
# exactly the error this would otherwise put on an EFB.
ADSB_COT_WITH_BARO = b"""<event version="2.0" uid="ICAO-A1B2C3" type="a-f-A-C-F"
  time="2026-07-15T00:00:00.000000Z" start="2026-07-15T00:00:00.000000Z"
  stale="2026-07-15T00:02:00.000000Z" how="m-g">
  <point lat="37.76" lon="-122.4" hae="1524.0" ce="9999999.0" le="9999999.0"/>
  <detail>
    <contact callsign="N123AB"/>
    <track course="90.0" speed="61.7"/>
    <__adsb alt_baro="4800" alt_geom="5000" flight="N123AB"/>
  </detail>
</event>"""

# readsb reports "ground" rather than a number for a target on the surface.
ADSB_COT_GROUND = ADSB_COT_WITH_BARO.replace(b'alt_baro="4800"', b'alt_baro="ground"')


def test_traffic_uses_pressure_altitude_when_available():
    """The whole point: alt_baro wins over hae for a Traffic Report."""
    track = gdltak.parse_cot(ADSB_COT_WITH_BARO)
    assert track["alt_press_ft"] == pytest.approx(4800.0)
    assert track["alt_ft"] == pytest.approx(4800.0)
    # ...and it is NOT the geometric value.
    assert track["alt_ft"] != pytest.approx(track["alt_geom_ft"])


def test_geometric_still_available_for_ownship():
    """Ownship Geometric Altitude legitimately wants the geometric value."""
    track = gdltak.parse_cot(ADSB_COT_WITH_BARO)
    assert track["alt_geom_ft"] == pytest.approx(5000.0, abs=1.0)


def test_falls_back_to_geometric_without_adsb_detail():
    """Non-adsbcot sources have no __adsb element; approximate beats dropping."""
    track = gdltak.parse_cot(ADSB_COT)
    assert track["alt_press_ft"] is None
    assert track["alt_ft"] == pytest.approx(5000.0, abs=1.0)


def test_ground_target_falls_back_rather_than_crashing():
    """readsb emits the string "ground"; it must not become a bogus altitude."""
    track = gdltak.parse_cot(ADSB_COT_GROUND)
    assert track["alt_press_ft"] is None
    assert track["alt_ft"] == pytest.approx(5000.0, abs=1.0)


def test_report_encodes_the_pressure_altitude():
    """End to end: the emitted GDL90 bytes differ from the geometric version."""
    baro = gdltak.track_to_report(gdltak.parse_cot(ADSB_COT_WITH_BARO))
    geom = gdltak.track_to_report(gdltak.parse_cot(ADSB_COT))
    assert baro != geom, "traffic report did not change when alt_baro was supplied"

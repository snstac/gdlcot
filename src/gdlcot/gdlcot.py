#!/usr/bin/env python3
"""
GDLCOT: Display TAK air pictures in ForeFlight — CoT to GDL90 gateway.

GDLCOT subscribes to Cursor on Target (default COT_URL=udp+ro://239.2.3.1:6969,
the AryaOS / ATAK Mesh SA multicast group), maintains a table of air tracks,
and broadcasts the picture as GDL90 over UDP (default
GDL90_URL=udp+broadcast://255.255.255.255:4000, the stratux/ForeFlight
convention) so Electronic Flight Bag apps — ForeFlight, FlyQ, Garmin Pilot —
display the same traffic that TAK sees. It is the reverse of adsbcot:
CoT in, GDL90 out.

Every 1/UPDATE_HZ seconds GDLCOT emits: a Heartbeat, an Ownship Report (from
the CoT track matching OWNSHIP_UID, or static OWNSHIP_LAT/OWNSHIP_LON), an
Ownship Geometric Altitude, and one Traffic Report per track fresher than
STALE_SECS.

Configuration is PyTAK-style via /etc/default/gdlcot (systemd EnvironmentFile)
or the environment: COT_URL, GDL90_URL, STALE_SECS, OWNSHIP_UID, OWNSHIP_LAT,
OWNSHIP_LON, OWNSHIP_ALT_FT, CALLSIGN, UPDATE_HZ.

Copyright Sensors & Signals LLC https://www.snstac.com/
SPDX-License-Identifier: Apache-2.0
"""

import asyncio
import configparser
import logging
import math
import os
import re
import socket
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zlib

import pytak

from gdlcot import gdl90

VERSION = "2.0.1"
logger = logging.getLogger("gdlcot")

DEFAULT_COT_URL = "udp+ro://239.2.3.1:6969"
DEFAULT_GDL90_URL = "udp+broadcast://255.255.255.255:4000"
DEFAULT_STALE_SECS = "60"
DEFAULT_UPDATE_HZ = "1"
DEFAULT_CALLSIGN = "GDLCOT"

M_PER_SEC_TO_KNOTS = 1.943844
METERS_TO_FEET = 3.28084

# CoT sentinel for "value unknown":
COT_NULL = 9999999.0

ICAO_UID_RE = re.compile(r"ICAO[-.]?([0-9A-Fa-f]{6})")

# GDL90 address types:
ADDR_TYPE_ICAO = 0
ADDR_TYPE_SELF_ASSIGNED = 1


def conf(key, default):
    return os.environ.get(key, default)


def _float_or_none(value):
    """Parse a CoT numeric attribute; None for missing/unparseable/sentinel."""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or abs(value) >= COT_NULL:
        return None
    return value


def cot_address(uid: str):
    """Derive a GDL90 participant address from a CoT UID.

    UIDs like "ICAO-A1B2C3" yield the real 24-bit ICAO address (address
    type 0); anything else gets a stable self-assigned (type 1) address
    hashed from the UID.
    """
    match = ICAO_UID_RE.search(uid)
    if match:
        return int(match.group(1), 16), ADDR_TYPE_ICAO
    return zlib.crc32(uid.encode("utf-8")) & 0xFFFFFF, ADDR_TYPE_SELF_ASSIGNED


def parse_cot(data):
    """Parse a CoT <event> into a track dict, or None if not usable.

    Extracts uid, type, point lat/lon/hae, detail/track course & speed
    (CoT speed is m/s; converted to knots) and detail/contact callsign.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, (bytes, bytearray)):
        return None
    try:
        event = ET.fromstring(data)
    except ET.ParseError:
        return None
    if event.tag != "event":
        return None

    uid = event.get("uid")
    point = event.find("point")
    if not uid or point is None:
        return None
    lat = _float_or_none(point.get("lat"))
    lon = _float_or_none(point.get("lon"))
    if lat is None or lon is None:
        return None

    # GDL90 Traffic Reports carry PRESSURE altitude, because that is what
    # every other aircraft's Mode C/S transponder reports and what an EFB needs
    # to compute relative altitude. The Ownship Geometric Altitude message is
    # the one that wants geometric.
    #
    # CoT's point/@hae is geometric (height above ellipsoid), so using it for a
    # Traffic Report mixes the two datums. The error is the geometric-minus-
    # pressure difference, which varies with local pressure and is routinely
    # hundreds of feet -- enough to make traffic appear at the wrong relative
    # level, which is the one number the display exists to show.
    #
    # adsbcot already publishes the right value: readsb's alt_baro, in feet,
    # carried through as <__adsb alt_baro="...">. Prefer it, and fall back to
    # geometric only when a source does not provide it (which is honest but
    # approximate, and better than dropping the track).
    hae = _float_or_none(point.get("hae"))
    alt_geom_ft = hae * METERS_TO_FEET if hae is not None else None

    course = None
    speed_kt = None
    callsign = None
    alt_press_ft = None
    detail = event.find("detail")
    if detail is not None:
        adsb = detail.find("__adsb")
        if adsb is not None:
            # readsb reports alt_baro in FEET already, so no conversion. A
            # ground target reports the string "ground" rather than a number;
            # _float_or_none returns None for it and the geometric fallback
            # applies.
            alt_press_ft = _float_or_none(adsb.get("alt_baro"))
        track = detail.find("track")
        if track is not None:
            course = _float_or_none(track.get("course"))
            speed = _float_or_none(track.get("speed"))
            if speed is not None and speed >= 0:
                speed_kt = speed * M_PER_SEC_TO_KNOTS
        contact = detail.find("contact")
        if contact is not None:
            callsign = contact.get("callsign")

    address, addr_type = cot_address(uid)
    return {
        "uid": uid,
        "cot_type": event.get("type", ""),
        "lat": lat,
        "lon": lon,
        # Traffic Reports use pressure altitude; geometric is kept separately
        # so the Ownship Geometric Altitude message can still use it.
        "alt_ft": alt_press_ft if alt_press_ft is not None else alt_geom_ft,
        "alt_press_ft": alt_press_ft,
        "alt_geom_ft": alt_geom_ft,
        "course": course,
        "speed_kt": speed_kt,
        "callsign": callsign or uid[:8],
        "address": address,
        "addr_type": addr_type,
    }


def is_air_track(cot_type: str) -> bool:
    """True if the CoT type is in the Air battle dimension (a-*-A...)."""
    parts = (cot_type or "").split("-")
    return len(parts) >= 3 and parts[0] == "a" and parts[2] == "A"


class TrackTable:
    """Latest CoT-derived state per UID, expiring stale entries."""

    def __init__(self, stale_secs: float, ownship_uid: str = ""):
        self.stale_secs = float(stale_secs)
        self.ownship_uid = ownship_uid
        self.tracks = {}

    def update_from_cot(self, data, now=None) -> bool:
        """Ingest one CoT event. Returns True if a track was updated.

        Only Air-dimension tracks are kept, except OWNSHIP_UID which is
        accepted regardless of type (e.g. an a-f-G self position).
        """
        track = parse_cot(data)
        if not track:
            return False
        if track["uid"] != self.ownship_uid and not is_air_track(track["cot_type"]):
            return False
        track["time"] = now if now is not None else time.monotonic()
        self.tracks[track["uid"]] = track
        return True

    def expire(self, now=None):
        """Drop tracks older than stale_secs."""
        now = now if now is not None else time.monotonic()
        cutoff = now - self.stale_secs
        for uid in [u for u, t in self.tracks.items() if t["time"] < cutoff]:
            del self.tracks[uid]

    def fresh(self, now=None):
        """Expire, then return current tracks."""
        self.expire(now)
        return list(self.tracks.values())

    def get(self, uid):
        return self.tracks.get(uid)


class Gdl90Sender:
    """UDP egress for framed GDL90 messages (broadcast or unicast)."""

    def __init__(self, url: str):
        parsed = urllib.parse.urlparse(url)
        scheme = (parsed.scheme or "udp").lower()
        if not scheme.startswith("udp"):
            raise ValueError(f"GDL90_URL must be udp[+broadcast]://, got: {url}")
        self.addr = (parsed.hostname or "255.255.255.255", parsed.port or 4000)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if "broadcast" in scheme or self.addr[0].endswith(".255"):
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    def send(self, message: bytes):
        """Frame and send one GDL90 message as its own datagram."""
        try:
            self.sock.sendto(gdl90.frame(message), self.addr)
        except OSError as exc:
            logger.debug("gdl90 send %s: %s", self.addr, exc)


def track_to_report(track: dict, msg_id: int = gdl90.MSG_TRAFFIC) -> bytes:
    """Build an unframed Traffic/Ownship Report from a track dict."""
    return gdl90.traffic_report(
        track["lat"],
        track["lon"],
        msg_id=msg_id,
        addr_type=track.get("addr_type", ADDR_TYPE_ICAO),
        address=track.get("address", 0),
        alt_ft=track.get("alt_ft"),
        hvel_kt=track.get("speed_kt"),
        track_deg=track.get("course") or 0.0,
        emitter=gdl90.emitter_category(track.get("cot_type", "")),
        callsign=track.get("callsign", ""),
    )


class CotWorker(pytak.QueueWorker):
    """Drain CoT events from the PyTAK rx_queue into the track table."""

    def __init__(self, queue, config, tracks, status):
        super().__init__(queue, config)
        self.tracks = tracks
        self.status = status

    async def handle_data(self, data) -> None:
        if self.tracks.update_from_cot(data):
            self.status.count("rx")
            self.status.set_input(
                last_observation=time.time(),
                total=self.status.as_dict()["counters"].get("rx", 0),
                tracked=len(self.tracks.tracks),
            )
            self.status.set_health("ok", "CoT air picture active")
            logger.debug("track update: %d tracks", len(self.tracks.tracks))

    async def run(self, _=-1):
        logger.info("subscribed to CoT at %s", self.config.get("COT_URL"))
        while True:
            data = await self.queue.get()
            await self.handle_data(data)


class Gdl90Worker(pytak.QueueWorker):
    """Emit the GDL90 picture (heartbeat/ownship/traffic) at UPDATE_HZ."""

    def __init__(self, queue, config, tracks, sender, status):
        super().__init__(queue, config)
        self.tracks = tracks
        self.sender = sender
        self.status = status

    def ownship_track(self):
        """Ownship state: live CoT track for OWNSHIP_UID, else static config."""
        ownship_uid = self.config.get("OWNSHIP_UID", "")
        if ownship_uid:
            track = self.tracks.get(ownship_uid)
            if track:
                return track
        lat = self.config.get("OWNSHIP_LAT", "")
        lon = self.config.get("OWNSHIP_LON", "")
        if lat and lon:
            return {
                "uid": ownship_uid,
                "lat": float(lat),
                "lon": float(lon),
                "alt_ft": float(self.config.get("OWNSHIP_ALT_FT", "") or 0.0),
                "callsign": self.config.get("CALLSIGN", DEFAULT_CALLSIGN),
                "address": 0,
                "addr_type": ADDR_TYPE_SELF_ASSIGNED,
                "cot_type": "a-f-A",
            }
        return None

    def beacon_once(self, now_wall=None):
        """Send one heartbeat + ownship + traffic cycle. Returns #messages."""
        now_wall = now_wall if now_wall is not None else time.time()
        sent = 0

        self.sender.send(gdl90.heartbeat(int(now_wall % 86400)))
        sent += 1

        ownship = self.ownship_track()
        if ownship:
            self.sender.send(track_to_report(ownship, msg_id=gdl90.MSG_OWNSHIP))
            sent += 1
            if ownship.get("alt_ft") is not None:
                self.sender.send(gdl90.ownship_geo_altitude(ownship["alt_ft"]))
                sent += 1

        ownship_uid = self.config.get("OWNSHIP_UID", "")
        for track in self.tracks.fresh():
            if ownship_uid and track["uid"] == ownship_uid:
                continue
            self.sender.send(track_to_report(track))
            sent += 1
        self.status.count("tx", sent)
        self.status.set_output(
            "connected",
            last_success=now_wall,
            total=self.status.as_dict()["counters"].get("tx", 0),
            destination=f"udp://{self.sender.addr[0]}:{self.sender.addr[1]}",
        )
        self.status.set(tracked=len(self.tracks.tracks))
        self.status.write()
        return sent

    async def run(self, _=-1):
        period = 1.0 / float(self.config.get("UPDATE_HZ", DEFAULT_UPDATE_HZ))
        logger.info(
            "emitting GDL90 to %s:%s every %.2fs",
            self.sender.addr[0],
            self.sender.addr[1],
            period,
        )
        while True:
            self.beacon_once()
            await asyncio.sleep(period)


async def run_cot_client(config, tracks, sender, status):
    """Build and run one CoT input attempt with fresh PyTAK transports."""
    clitool = pytak.CLITool(config)
    await clitool.setup()
    clitool.add_tasks(
        {
            CotWorker(clitool.rx_queue, config, tracks, status),
            Gdl90Worker(clitool.tx_queue, config, tracks, sender, status),
        }
    )
    await clitool.run()


async def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s gdlcot %(levelname)s %(message)s",
    )
    parser = configparser.ConfigParser()
    parser.read_dict(
        {
            "gdlcot": {
                "COT_URL": conf("COT_URL", DEFAULT_COT_URL),
                "GDL90_URL": conf("GDL90_URL", DEFAULT_GDL90_URL),
                "STALE_SECS": conf("STALE_SECS", DEFAULT_STALE_SECS),
                "OWNSHIP_UID": conf("OWNSHIP_UID", ""),
                "OWNSHIP_LAT": conf("OWNSHIP_LAT", ""),
                "OWNSHIP_LON": conf("OWNSHIP_LON", ""),
                "OWNSHIP_ALT_FT": conf("OWNSHIP_ALT_FT", ""),
                "CALLSIGN": conf("CALLSIGN", DEFAULT_CALLSIGN),
                "UPDATE_HZ": conf("UPDATE_HZ", DEFAULT_UPDATE_HZ),
                "PYTAK_NO_HELLO": "1",
            }
        }
    )
    config = parser["gdlcot"]
    # Pass through PYTAK_* (TLS etc.) from the environment.
    for key, val in os.environ.items():
        if key.startswith("PYTAK_"):
            config[key] = val

    tracks = TrackTable(float(config.get("STALE_SECS")), config.get("OWNSHIP_UID", ""))
    sender = Gdl90Sender(config.get("GDL90_URL"))
    status = pytak.StatusWriter("gdlcot", version=VERSION)
    status.set_health("degraded", "waiting for CoT air tracks")
    status.set_input(connection="connected")
    status.set_output(
        "connected",
        destination=f"udp://{sender.addr[0]}:{sender.addr[1]}",
    )
    status.write(force=True)

    await pytak.supervise_with_reconnect(
        config,
        lambda: run_cot_client(config, tracks, sender, status),
    )


def cli_main():
    """Console script entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    cli_main()

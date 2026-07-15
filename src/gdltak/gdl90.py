#!/usr/bin/env python3
"""Pure-stdlib GDL90 message encoder.

Implements the subset of GARMIN GDL 90 Data Interface Specification
(560-1058-00 Rev A) needed to feed Electronic Flight Bag apps:

- Heartbeat (message ID 0x00)
- Ownship Report (0x0A) / Traffic Report (0x14) — identical 28-byte layout
- Ownship Geometric Altitude (0x0B)
- CRC-16-CCITT (poly 0x1021, init 0x0000, table-driven) and byte-stuffed
  0x7E framing.

Copyright Sensors & Signals LLC https://www.snstac.com/
SPDX-License-Identifier: Apache-2.0
"""

from typing import Optional

FLAG = 0x7E
ESCAPE = 0x7D

MSG_HEARTBEAT = 0x00
MSG_OWNSHIP = 0x0A
MSG_OWNSHIP_GEO_ALT = 0x0B
MSG_TRAFFIC = 0x14

# Miscellaneous indicator: airborne + true-track valid.
MISC_AIRBORNE_TRUE_TRACK = 0b1001

# GDL90 emitter categories (subset).
EMITTER_NONE = 0
EMITTER_LIGHT = 1
EMITTER_ROTORCRAFT = 7
EMITTER_GLIDER = 9
EMITTER_LIGHTER_THAN_AIR = 10
EMITTER_UAV = 14

# CoT type fragment -> GDL90 emitter category. First match wins; the
# affiliation atom (f/h/n/u...) is ignored on purpose.
COT_TYPE_TO_EMITTER = (
    ("-A-M-F-Q", EMITTER_UAV),  # military fixed-wing drone / UAS
    ("-A-C-U", EMITTER_UAV),  # civil UAS
    ("-A-C-H", EMITTER_ROTORCRAFT),  # civil rotorcraft
    ("-A-M-H", EMITTER_ROTORCRAFT),  # military rotorcraft
    ("-A-C-G", EMITTER_GLIDER),  # glider
    ("-A-C-L", EMITTER_LIGHTER_THAN_AIR),  # lighter than air
)


def _make_crc_table():
    """Build the CRC-16-CCITT (poly 0x1021, MSB-first) lookup table."""
    table = []
    for i in range(256):
        crc = (i << 8) & 0xFFFF
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
        table.append(crc)
    return tuple(table)


CRC16_TABLE = _make_crc_table()


def crc16(data: bytes) -> int:
    """CRC-16-CCITT of `data`, init 0x0000, as used by GDL90 (spec §2.2.3)."""
    crc = 0
    for byte in data:
        crc = (CRC16_TABLE[crc >> 8] ^ ((crc << 8) & 0xFFFF) ^ byte) & 0xFFFF
    return crc


def byte_stuff(data: bytes) -> bytes:
    """Escape 0x7E/0x7D as 0x7D + (byte XOR 0x20) (spec §2.2.1)."""
    out = bytearray()
    for byte in data:
        if byte in (FLAG, ESCAPE):
            out.append(ESCAPE)
            out.append(byte ^ 0x20)
        else:
            out.append(byte)
    return bytes(out)


def byte_unstuff(data: bytes) -> bytes:
    """Reverse `byte_stuff()`."""
    out = bytearray()
    escaped = False
    for byte in data:
        if escaped:
            out.append(byte ^ 0x20)
            escaped = False
        elif byte == ESCAPE:
            escaped = True
        else:
            out.append(byte)
    return bytes(out)


def frame(message: bytes) -> bytes:
    """Frame a GDL90 message: append CRC (low byte first), stuff, add flags."""
    crc = crc16(message)
    payload = message + bytes((crc & 0xFF, (crc >> 8) & 0xFF))
    return bytes((FLAG,)) + byte_stuff(payload) + bytes((FLAG,))


def deframe(framed: bytes) -> Optional[bytes]:
    """Reverse `frame()`: unwrap flags, unstuff, verify & strip CRC.

    Returns the message bytes, or None if framing/CRC is invalid.
    """
    data = framed.strip(bytes((FLAG,)))
    payload = byte_unstuff(data)
    if len(payload) < 3:
        return None
    message, crc_bytes = payload[:-2], payload[-2:]
    crc = crc_bytes[0] | (crc_bytes[1] << 8)
    if crc != crc16(message):
        return None
    return message


def _pack24(value: int) -> bytes:
    """24-bit big-endian, two's complement for negative values."""
    value &= 0xFFFFFF
    return bytes(((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF))


def encode_latlon(degrees: float) -> int:
    """Encode degrees as 24-bit two's-complement semicircles (LSB=180/2^23).

    Returns the raw 24-bit field value (0..0xFFFFFF).
    """
    semicircles = int(round(degrees * (1 << 23) / 180.0))
    semicircles = max(-(1 << 23), min((1 << 23) - 1, semicircles))
    return semicircles & 0xFFFFFF


def encode_altitude(alt_ft: Optional[float]) -> int:
    """Encode pressure altitude as 12-bit field: (ft + 1000) / 25, 0xFFF invalid."""
    if alt_ft is None:
        return 0xFFF
    encoded = int(round((alt_ft + 1000) / 25.0))
    return max(0, min(0xFFE, encoded))


def encode_hvelocity(knots: Optional[float]) -> int:
    """Encode horizontal velocity as 12-bit knots field, 0xFFF invalid."""
    if knots is None or knots < 0:
        return 0xFFF
    return min(0xFFE, int(round(knots)))


def encode_vvelocity(fpm: Optional[float]) -> int:
    """Encode vertical velocity as signed 12-bit field of 64 fpm, 0x800 invalid."""
    if fpm is None:
        return 0x800
    encoded = int(round(fpm / 64.0))
    encoded = max(-510, min(510, encoded))
    return encoded & 0xFFF


def encode_track(degrees: float) -> int:
    """Encode track/heading as 8-bit field (LSB = 360/256 degrees)."""
    return int(round((degrees % 360.0) * 256.0 / 360.0)) & 0xFF


def emitter_category(cot_type: str) -> int:
    """Map a CoT type (e.g. a-f-A-C-H) to a GDL90 emitter category."""
    for fragment, category in COT_TYPE_TO_EMITTER:
        if fragment in cot_type:
            return category
    return EMITTER_LIGHT


def heartbeat(
    ts_seconds: int,
    status1: int = 0x81,
    status2: int = 0x00,
    uplink_count: int = 0,
    basic_long_count: int = 0,
) -> bytes:
    """Heartbeat message (ID 0x00, spec §3.1), unframed.

    `ts_seconds` is seconds since UTC midnight (17 bits: bit 16 is carried
    in status2 bit 7, low 16 bits little-endian).
    Default status1 0x81 = UAT initialized + UTC timing valid.
    """
    ts_seconds = int(ts_seconds) & 0x1FFFF
    status2 = (status2 & 0x7F) | (((ts_seconds >> 16) & 0x01) << 7)
    return bytes(
        (
            MSG_HEARTBEAT,
            status1 & 0xFF,
            status2,
            ts_seconds & 0xFF,
            (ts_seconds >> 8) & 0xFF,
            ((uplink_count & 0x1F) << 3) | ((basic_long_count >> 8) & 0x07),
            basic_long_count & 0xFF,
        )
    )


def traffic_report(
    lat: float,
    lon: float,
    *,
    msg_id: int = MSG_TRAFFIC,
    alert: int = 0,
    addr_type: int = 0,
    address: int = 0,
    alt_ft: Optional[float] = None,
    misc: int = MISC_AIRBORNE_TRUE_TRACK,
    nic: int = 8,
    nacp: int = 8,
    hvel_kt: Optional[float] = None,
    vvel_fpm: Optional[float] = None,
    track_deg: float = 0.0,
    emitter: int = EMITTER_LIGHT,
    callsign: str = "",
    priority: int = 0,
) -> bytes:
    """Traffic Report (ID 0x14) / Ownship Report (ID 0x0A), unframed.

    28-byte layout per spec §3.5.1: both messages are identical except for
    the message ID (pass msg_id=MSG_OWNSHIP for ownship).
    """
    lat_enc = encode_latlon(lat)
    lon_enc = encode_latlon(lon)
    alt_enc = encode_altitude(alt_ft)
    hvel_enc = encode_hvelocity(hvel_kt)
    vvel_enc = encode_vvelocity(vvel_fpm)
    callsign_bytes = callsign.encode("ascii", "replace")[:8].ljust(8, b" ")

    msg = bytearray()
    msg.append(msg_id & 0xFF)
    msg.append(((alert & 0x0F) << 4) | (addr_type & 0x0F))
    msg.extend(_pack24(address))
    msg.extend(_pack24(lat_enc))
    msg.extend(_pack24(lon_enc))
    msg.append((alt_enc >> 4) & 0xFF)
    msg.append(((alt_enc & 0x0F) << 4) | (misc & 0x0F))
    msg.append(((nic & 0x0F) << 4) | (nacp & 0x0F))
    msg.append((hvel_enc >> 4) & 0xFF)
    msg.append(((hvel_enc & 0x0F) << 4) | ((vvel_enc >> 8) & 0x0F))
    msg.append(vvel_enc & 0xFF)
    msg.append(encode_track(track_deg))
    msg.append(emitter & 0xFF)
    msg.extend(callsign_bytes)
    msg.append((priority & 0x0F) << 4)
    return bytes(msg)


def ownship_report(lat: float, lon: float, **kwargs) -> bytes:
    """Ownship Report (ID 0x0A), unframed. Same fields as `traffic_report()`."""
    return traffic_report(lat, lon, msg_id=MSG_OWNSHIP, **kwargs)


def ownship_geo_altitude(
    geo_alt_ft: float, vertical_metrics: int = 0x7FFF
) -> bytes:
    """Ownship Geometric Altitude (ID 0x0B, spec §3.8), unframed.

    Altitude is signed 16-bit in 5-ft units; vertical metrics 0x7FFF means
    Vertical Figure of Merit unavailable, no vertical warning.
    """
    alt = int(round(geo_alt_ft / 5.0))
    alt = max(-(1 << 15), min((1 << 15) - 1, alt)) & 0xFFFF
    return bytes(
        (
            MSG_OWNSHIP_GEO_ALT,
            (alt >> 8) & 0xFF,
            alt & 0xFF,
            (vertical_metrics >> 8) & 0xFF,
            vertical_metrics & 0xFF,
        )
    )

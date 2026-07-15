# GDLTAK: Display TAK Air Pictures in ForeFlight — CoT to GDL90 Gateway

GDLTAK broadcasts the local **Cursor on Target (CoT)** air picture as
**GDL90** over UDP, so Electronic Flight Bag (EFB) apps — **ForeFlight**,
**FlyQ EFB**, **Garmin Pilot** — display the same traffic that TAK sees.
It is the reverse of [adsbcot](https://github.com/snstac/adsbcot):
CoT in, GDL90 out.

GDLTAK subscribes to CoT (by default the AryaOS / ATAK **Mesh SA**
multicast group), keeps a table of air tracks, and once a second emits
GDL90 Heartbeat, Ownship and Traffic Report datagrams the way a stratux
or GDL 90 receiver would. Any traffic feeding your TAK network — ADS-B via
adsbcot, drone Remote ID via dronecot, tracks from a TAK Server — shows up
in the cockpit.

## ForeFlight setup

1. Put the iPad/iPhone on the same Wi-Fi network as the device running
   GDLTAK (e.g. join the AryaOS hotspot).
2. That's it — ForeFlight auto-detects GDL90 traffic on UDP port 4000 and
   lists it under **More → Devices**. FlyQ and Garmin Pilot behave the same.

## Installation

On AryaOS / Debian, from the [snstac package repo](https://snstac.github.io/packages):

```sh
sudo apt install gdltak
sudo systemctl enable --now gdltak
```

Or from source:

```sh
python3 -m pip install gdltak
```

The Debian package installs:

- `/usr/bin/gdltak`
- `/etc/default/gdltak`
- `/lib/systemd/system/gdltak.service` (ships disabled; enable as above)

## Configuration

PyTAK-style, via `/etc/default/gdltak` (systemd EnvironmentFile), the
environment, or an INI file with a `[gdltak]` section:

| Key | Default | Description |
|---|---|---|
| `COT_URL` | `udp+ro://239.2.3.1:6969` | CoT source (PyTAK URL). Default is the Mesh SA multicast group. |
| `GDL90_URL` | `udp+broadcast://255.255.255.255:4000` | GDL90 egress. Broadcast is the stratux/ForeFlight convention; unicast `udp://host:port` also works. |
| `STALE_SECS` | `60` | Drop tracks not updated within this many seconds. |
| `UPDATE_HZ` | `1` | GDL90 update rate (heartbeat convention is 1 Hz). |
| `OWNSHIP_UID` | — | CoT UID whose track becomes the Ownship Report (e.g. this device's gpstak/lincot UID). |
| `OWNSHIP_LAT` / `OWNSHIP_LON` / `OWNSHIP_ALT_FT` | — | Static ownship position fallback. If no ownship is configured, GDLTAK sends heartbeat + traffic only. |
| `CALLSIGN` | `GDLTAK` | Ownship callsign shown in the EFB. |

`PYTAK_*` options (TLS client certs, etc.) are passed through to PyTAK,
so any PyTAK-supported CoT source works, including TAK Server over TLS.

Notes:

- CoT carries geometric altitude (HAE); GDLTAK uses it for both the
  pressure-altitude field of Traffic Reports and the Ownship Geometric
  Altitude message. EFBs treat it as advisory traffic, not certified
  ADS-B In.
- Tracks with UIDs like `ICAO-A1B2C3` (adsbcot convention) keep their
  real 24-bit ICAO address; other tracks get a stable self-assigned
  address hashed from the UID.

## Software Suite

GDLTAK is part of the [snstac](https://github.com/snstac) TAK gateway
family, built on [PyTAK](https://github.com/snstac/pytak) and
pre-installed on [AryaOS](https://github.com/snstac/aryaos):
[adsbcot](https://github.com/snstac/adsbcot) (aircraft via ADS-B),
[aiscot](https://github.com/snstac/aiscot) (ships via AIS),
[dronecot](https://github.com/snstac/dronecot) (drone Remote ID),
[lincot](https://github.com/snstac/lincot) /
[gpstak](https://github.com/snstac/gpstak) (own position via GNSS),
[aprscot](https://github.com/snstac/aprscot) (APRS amateur radio),
[windtak](https://github.com/snstac/windtak) (weather stations) and
[charontak](https://github.com/snstac/charontak) (CoT routing).

## Development

```sh
make editable install_test_requirements
make pytest
make package   # Debian package via stdeb
```

## License

Copyright Sensors & Signals LLC — Apache License, Version 2.0.

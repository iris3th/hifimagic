# StreamMagic controller for Linux (CXN100)

A small, dependency-free Linux application for Cambridge Audio network players
(CXN100 and other StreamMagic devices). It talks to the player over its local
SMoIP WebSocket protocol — the same protocol the iOS/Android app uses for
device control — and presents it as a desktop-style web interface.

## What it does

- **Now Playing** — cover art (when available), track/artist/album, live progress
  and seek, play/pause/stop, previous/next, shuffle and repeat, signal details
  (codec, sample rate, bit depth, bitrate, lossless/MQA).
- **Sources** — switch between Internet Radio, Media Library, AirPlay, Spotify,
  Coax/Optical, USB Audio, Google Cast, Roon, TIDAL/Qobuz Connect, etc. Only
  sources the device marks as selectable are shown.
- **Presets** — play, rename and delete saved presets, with search.
- **Play queue** — browse the current queue and jump to a track.
- **Internet Radio** — play a stream by URL, or recall saved Airable stations.
- **Settings** — volume/pre-amp/control bus, volume limit, 7-band EQ with
  presets, room correction (tilt EQ), audio output, display brightness,
  standby mode, auto power-down, device name, firmware update status/check,
  early-update channel, and network information.
- **Live updates** — the UI follows the player in real time via Server-Sent
  Events, including changes made with the physical remote or the official app.

## What it deliberately does not do

- Spotify/TIDAL/Qobuz **login and content browsing**: those features require
  vendor developer credentials and cloud APIs. This app lets you select those
  sources and control playback once a session is started from the device or
  the official app.
- **Media Library folder browsing**: this is not exposed by the documented
  SMoIP control protocol (the vendor app uses separate services for it). The
  current queue is available.
- Firmware **installation**, factory reset and reboot are not exposed. Only a
  firmware *check* is available.

## Safety

The local server binds to `127.0.0.1` by default. The browser can only call a
strict allow-list of validated commands — arbitrary device paths are never
forwarded. Destructive actions (preset deletion) ask for confirmation. No
device configuration is changed just by starting the app; startup only reads
state.

## Run on Linux

Requires Python 3.9 or newer (no pip, no third-party modules):

```bash
python3 cxn100.py
```

Then open <http://127.0.0.1:8080>. Useful options:

```bash
python3 cxn100.py --host 192.168.1.20 --port 8080 --no-browser
python3 cxn100.py --bind 0.0.0.0   # expose the UI to other machines on your LAN
python3 cxn100.py --verbose        # log protocol traffic details
```

If the player is in Eco standby mode its network interface is off. Set
**Standby Mode → Network standby** on the device (or in this app while it is
awake) if you want to control it while it is in standby.

## Portable AppImage

See [APPIMAGE.md](APPIMAGE.md) for building a single-file AppImage. The build
script downloads an AppImage-compatible Python runtime and `appimagetool`, then
produces `StreamMagic-CXN100-x86_64.AppImage`.

## Files

- `cxn100.py` — command-line entry point
- `smoip.py` — stdlib WebSocket/SMoIP client with reconnection
- `server.py` — state mirror, command allow-list, HTTP + SSE server
- `web/` — the browser UI (plain HTML/CSS/JS, no build step)
- `build-appimage.sh` — portable AppImage builder
- `cxn100.desktop` — optional desktop launcher template

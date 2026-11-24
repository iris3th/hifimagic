# HifiMagic controller for Linux and macOS (CXN100)

A small, dependency-free application for Cambridge Audio network players
(CXN100 and compatible models). It talks to the player over its local
SMoIP WebSocket protocol — the same protocol the iOS/Android app uses for
device control, plus the player's local UPnP `ContentDirectory` service for USB
library browsing — and presents it as a desktop-style web interface.

The app always starts, even if the player is off or unreachable. It opens on a
splash screen with the HifiMagic logo and **only** two fields — player IP address
and port (default `80`) — plus a **Connect** button. Click the connection badge
at any time to reopen it.

## What it does

- **Now Playing** — cover art (when available), track/artist/album, live progress
  and seek, play/pause/stop, previous/next, shuffle and repeat, signal details
  (codec, sample rate, bit depth, bitrate, lossless/MQA).
- **Sources** — switch between Internet Radio, Media Library, AirPlay, Spotify,
  Coax/Optical, USB Audio, Google Cast, Roon, TIDAL/Qobuz Connect, etc. Only
  sources the device marks as selectable are shown.
- **Presets** — play, rename and delete saved presets, with search.
- **Play queue** — browse the current queue and jump to a track; add any track to your playlist.
- **USB / Library** — browse the attached USB drive folder by folder using the player's local
  UPnP server (port `8050`), open albums and tracks, add them to the player queue or save them
  in a playlist for offline/local use.
- **Playlist** — build a named, re-orderable playlist from USB folders/albums/tracks, queue
  tracks, presets or stream URLs;
  it is saved in the browser and can be played back in order.
- **Internet Radio** — play a stream by URL, recall saved Airable stations, or add either to a playlist.
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
- Firmware **installation**, factory reset and reboot are not exposed. Only a
  firmware *check* is available.

## Safety

The local server binds to `127.0.0.1` by default. The browser can only call a
strict allow-list of validated commands — arbitrary device paths are never
forwarded. USB browsing uses read-only UPnP `Browse` calls. Queue commands are
built from validated library object IDs and DIDL metadata, never from arbitrary
browser-supplied paths. Destructive actions (preset deletion) ask for
confirmation. No device configuration is changed just by starting the app;
startup only reads state.

## Run from source (Linux or macOS)

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

To show a small, unobtrusive **Support** link in the footer, set an http(s) URL:

```bash
HIFIMAGIC_SUPPORT_URL=https://your-payment-link.example/iris3th python3 cxn100.py
# or
python3 cxn100.py --support-url https://your-payment-link.example/iris3th
```

When running from source it is hidden by default, so the interface stays
clean. The AppImage and macOS app ship with a default support URL, but you can
override it or hide it:

```bash
HIFIMAGIC_SUPPORT_URL=https://your-link.example ./HifiMagic-CXN100-x86_64.AppImage
HIFIMAGIC_SUPPORT_URL= ./HifiMagic-CXN100-x86_64.AppImage   # hide it
```

`--support-url` works in the desktop apps too.

### Stripe support link

The recommended setup is a single Stripe-hosted **Payment Link** with a
customer-chosen EUR amount (preset €5, minimum €1, maximum €50). It is one-time
only, needs no webhook or backend, and keeps your secret key out of the
distributed app. The publishable key is not needed for a hosted redirect.

A test link is available for verification:

```text
https://buy.stripe.com/test_6oU4gtaH1grI4FJbVv1B600
```

Test it with:

```bash
HIFIMAGIC_SUPPORT_URL=https://buy.stripe.com/test_6oU4gtaH1grI4FJbVv1B600 python3 cxn100.py
```

The shipped desktop apps use this live link by default:

```text
https://buy.stripe.com/bJebIU5en43t2EY43Dawo00
```

To create a new live link, put a live key in `.env` and run:

```bash
./create-support-link.sh --live
```

A restricted key (`rk_live_...`) is preferred over a full secret key. Grant it
only **Products**, **Prices**, and **Payment Links** with read/write access;
nothing else is needed. A full `sk_live_...` key works too, but is riskier if
leaked. The key is used only when creating the link, never by the app itself.

The script prints only public IDs and the URL. Set `HIFIMAGIC_SUPPORT_URL` to
that live URL in your launcher or environment. In the Stripe Dashboard you can
create the same thing with **Payment Links → New → Customers choose what to
pay**, currency EUR. The link disables Managed Payments so no product tax code
is required for these optional support payments.

USB library browsing reads the player's UPnP server on port `8050`; the server
UDN is discovered from `/smoip/system/upnp` at connect time. No phone app,
proxy, or vendor cloud account is needed.

If the player is in Eco standby mode its network interface is off. Set
**Standby Mode → Network standby** on the device (or in this app while it is
awake) if you want to control it while it is in standby.

## Portable AppImage (Linux x86_64)

See [APPIMAGE.md](APPIMAGE.md) for building a single-file AppImage. The build
script downloads an AppImage-compatible Python runtime and `appimagetool`, then
produces `HifiMagic-CXN100-x86_64.AppImage`.

## Apple Silicon (macOS arm64)

Download `HifiMagic-CXN100-apple-silicon.dmg` (or the `.zip`) from the
[releases](https://github.com/iris3th/hifimagic/releases) page. It is a native
arm64 `.app` with its own Python 3.11 runtime bundled — no Homebrew, pip, Xcode
or system Python is needed. macOS 11 or newer is required.

Because the build is not notarised, a downloaded copy may be quarantined. Open
it with **Right-click → Open**, or remove the quarantine flag:

```bash
xattr -d com.apple.quarantine /Applications/HifiMagic.app
```

To build it yourself on an Apple Silicon Mac:

```bash
./build-macos-app.sh
```

The script writes `HifiMagic-CXN100-apple-silicon.zip` and, using the
built-in `hdiutil`, `HifiMagic-CXN100-apple-silicon.dmg`. It can also be run on
a non-macOS build machine to produce the `.zip`, but the DMG step is skipped.

## Automated releases

`.github/workflows/appimage.yml` builds both desktop apps and attaches them to a
GitHub Release. Push a version tag:

```bash
git tag v1.0.1
git push origin v1.0.1
```

The workflow creates or updates that release with:

- `HifiMagic-CXN100-x86_64.AppImage`
- `HifiMagic-CXN100-apple-silicon.zip`
- `HifiMagic-CXN100-apple-silicon.dmg`
- `SHA256SUMS-v1.0.1.txt`

You can also run the workflow manually from the Actions tab and enter the
release tag. Binaries are stored as **release assets attached to the Git tag**,
not committed into the source tree.

## Files

- `cxn100.py` — command-line entry point
- `smoip.py` — stdlib WebSocket/SMoIP client with reconnection
- `upnp.py` — stdlib UPnP `ContentDirectory` browser and DIDL queue helper
- `server.py` — state mirror, command allow-list, HTTP + SSE server
- `web/` — the browser UI (plain HTML/CSS/JS, no build step)
- `build-appimage.sh` — portable Linux AppImage builder
- `build-macos-app.sh` — Apple Silicon `.app`/DMG builder
- `cxn100.desktop` — optional Linux desktop launcher template

## License

HifiMagic is free software licensed under the **GNU Affero General Public
License, version 3 or (at your option) any later version**
(`AGPL-3.0-or-later`). See [LICENSE](LICENSE) for the full text; source files
carry an `SPDX-License-Identifier: AGPL-3.0-or-later` header.

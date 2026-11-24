# HifiMagic media manager for CXN100 (Linux and macOS)

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
- **Play queue** — browse the current queue, jump to a track, add any track to
  your playlist, save the whole queue as a named playlist, or clear the queue
  (with confirmation).
- **USB / Library** — browse the attached USB drive folder by folder using the player's local
  UPnP server (port `8050`). Every album and folder has a **⋮** menu with Play now, Play next,
  Add to player queue, and Add to playlist (existing or new). Empty artwork/scan folders are
  hidden only after their contents are checked, so an album really called "Artwork" is still shown.
- **Playlist** — build a named, re-orderable playlist from USB folders/albums/tracks, queue
  tracks, presets or stream URLs. Save it under any name, then load, delete or
  re-save it from the dropdown. **Play all** queues the library items in order and starts the
  first one; it no longer starts every track in turn. Playlists are stored on this computer,
  not in a browser profile or in the cloud.
- **Discover** — a local, searchable index of the USB library. Type
  `christmas songs`, `artist:Enya`, `"jingle bells"` or `genre:jazz`, then play a
  result or add the whole set to the current playlist. Save a search as a
  **smart playlist** to have it refresh automatically after a library re-scan.
  Search and smart playlists work entirely offline. Optionally, an
  OpenAI-compatible endpoint can interpret natural-language requests; it is
  never contacted unless you supply a key.
- **Internet Radio** — play a stream URL or add it to a playlist. Stored radio
  stations are managed once, under Presets, rather than duplicated here.
- **Settings** — volume/pre-amp/control bus, volume limit, 7-band EQ with
  presets, room correction (tilt EQ), audio output, display brightness,
  standby mode, auto power-down, device name, firmware update status/check,
  early-update channel, and network information.
- **Live updates** — the UI follows the player in real time via Server-Sent
  Events, including changes made with the physical remote or the official app.
- **Command palette** — press `Ctrl+K` / `Cmd+K` or the ⌕ button to search the
  local library and run commands without leaving the keyboard.
- **Mini player** — collapse the interface to a compact art/transport/volume
  dock with the title-bar button or `Ctrl+M` / `Cmd+M`. **↗** opens it in a
  small separate browser window that can be left on top or minimized.

## What it deliberately does not do

- Spotify/TIDAL/Qobuz **login and content browsing**: those features require
  vendor developer credentials and cloud APIs. This app lets you select those
  sources and control playback once a session is started from the device or
  the official app.
- Firmware **installation**, factory reset and reboot are not exposed. Only a
  firmware *check* is available.
- HifiMagic never writes playlists or index files to the streamer's USB storage.
  The USB drive is read-only to the app; playlists live in the local data folder
  listed below, and playback uses the player's own queue.

## Safety

The local server binds to `127.0.0.1` by default. The browser can only call a
strict allow-list of validated commands — arbitrary device paths are never
forwarded. USB browsing uses read-only UPnP `Browse` calls. Queue commands are
built from validated library object IDs and DIDL metadata, never from arbitrary
browser-supplied paths. Destructive actions (preset deletion) ask for
confirmation. No device configuration is changed just by starting the app;
startup only reads state.

## Where playlists and settings live

HifiMagic writes no user data to the cloud and does not use your browser profile
as the source of truth. The local server stores:

| Platform | Directory |
| --- | --- |
| Linux | `~/.local/share/hifimagic/` (or `$XDG_DATA_HOME/hifimagic/`) |
| macOS | `~/Library/Application Support/HifiMagic/` |

Files in that directory:

- `playlists.json` — named static and smart playlists.
- `library-index.json` — metadata from the read-only USB library scan.

Connection details (player IP and port) are kept in memory for the current run;
the browser only caches the playlist you are currently editing, so playlists
survive a browser reset. Back up the directory above to keep them safe.

Optional AI query interpretation is disabled by default. To enable it, set
`HIFIMAGIC_AI_KEY` before starting the app, and optionally
`HIFIMAGIC_AI_URL` and `HIFIMAGIC_AI_MODEL` for an OpenAI-compatible service.
Without a key, all search stays local.

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
HIFIMAGIC_SUPPORT_URL=https://your-link.example ./HifiMagic-<version>-x86_64.AppImage
HIFIMAGIC_SUPPORT_URL= ./HifiMagic-<version>-x86_64.AppImage   # hide it
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
produces `HifiMagic-<version>-x86_64.AppImage`. The version comes from the
`VERSION` file, or from the `VERSION` environment variable if set.

## Apple Silicon (macOS arm64)

Download `HifiMagic-<version>-apple-silicon.dmg` (or the `.zip`) from the
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

The script writes `HifiMagic-<version>-apple-silicon.zip` and, using the
built-in `hdiutil`, `HifiMagic-<version>-apple-silicon.dmg`. It can also be run on
a non-macOS build machine to produce the `.zip`, but the DMG step is skipped.

## Automated releases

`.github/workflows/appimage.yml` builds both desktop apps and attaches them to a
GitHub Release. Push a version tag:

```bash
git tag v1.0.1
git push origin v1.0.1
```

The workflow creates or updates that release with:

- `HifiMagic-<version>-x86_64.AppImage`
- `HifiMagic-<version>-apple-silicon.zip`
- `HifiMagic-<version>-apple-silicon.dmg`
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

# HifiMagic — local controller for Cambridge Audio StreamMagic players

A small, dependency-free desktop-style application for Cambridge Audio
StreamMagic network players. It talks to the player over its local SMoIP
WebSocket protocol — the same protocol the iOS/Android app uses for device
control — plus the player's local UPnP `ContentDirectory` service where
available, and presents it as a clean web interface.

HifiMagic is device-family based, not CXN100-only. It uses the player's own
reported capabilities and hides controls the connected player does not support.
The CXN100 is the reference device; other StreamMagic players should work to the
extent their firmware exposes the same endpoints.

The app always starts, even if the player is off or unreachable. On launch it
uses the last player address saved in `~/.local/share/hifimagic/config.json`.
If there is no saved address it performs a read-only network scan; if that finds
nothing, the splash screen shows the player IP and port (default `80`) plus a
**Find players on my network** button. Click the connection badge at any time to
reopen that screen. The web controller uses port `8765` by default and moves to
the next free port if `8765` is busy.

## Why use HifiMagic

HifiMagic is not a streaming service, a phone app duplicated on a desktop, or an
always-on media server. It is a focused controller for someone who already has a
Cambridge Audio StreamMagic player and wants a better local desk experience.

- **Local first.** No account, no cloud login, no vendor developer credentials,
  no third-party Python/Node packages, and no browser profile as the source of
  truth. Your settings, playlists, notes and radio favourites are JSON files in
  one known local folder, with verified backup and restore.
- **Desktop behaviour.** It opens when you launch it and exits when the last
  browser tab closes. There is no always-on server, NAS or Raspberry Pi to keep
  running. A true mini-player window is available, but the full UI is the point.
- **Real library tools.** Search by title, artist, album or genre, save smart
  playlists, organise static playlists, re-resolve saved tracks after a USB
  remount, and run a read-only deep scan for broken entries. These are the tools
  a large local collection actually needs.
- **Honest state.** The header distinguishes **Connected · On** from
  **Connected · Standby**. Deliberate actions wake a network-standby player;
  passive polling never does. Cached USB metadata is discarded and re-read after
  waking, so the library reflects the drive that is actually attached.
- **Honest features.** Controls are hidden when the connected device does not
  expose them. There is no animated level meter unless the player supplies a real
  level feed; a dead meter is not “evidence” of audio.
- **Internet radio without a cloud account.** The public `radio-browser.info`
  directory is contacted only when you search. Results and saved stations are
  cached locally, and device presets stay under the player's own Presets view.
- **Metadata without an account.** Local library tags are always used first and
  need no network. MusicBrainz is built in; a custom JSON endpoint can be added
  for a Plex-like local metadata proxy. AllMusic and Last.fm are not built in
  because they need keys or are deprecated.
- **Safer by design.** Localhost binding by default, strict command allowlist,
  validated SMOIP/UPnP paths, read-only USB browsing, and no writes to the
  streamer's storage.

The official StreamMagic app remains the right tool for vendor services,
multi-device setup, lock-screen controls and features implemented inside the
player. HifiMagic is the better tool when you want a fast, local, keyboard-
friendly desktop controller with serious library management and recoverable
local data.

## Download

Ready-made desktop builds are on the
[HifiMagic Releases page](https://github.com/iris3th/hifimagic/releases/latest).
Download the Linux `x86_64.AppImage` or the Apple Silicon `.zip` / `.dmg`, and
check the matching `SHA256SUMS-vX.Y.Z.txt` asset if you verify downloads.
The footer always shows the running version (for example `v1.0.18`), read from
the installed `VERSION` file rather than from the browser cache.

## What it does

- **Now Playing** — cover art (when available), track/artist/album, live progress
  and seek, play/pause/stop, previous/next, shuffle and repeat, signal details
  (codec, sample rate, bit depth, bitrate, lossless/MQA). For internet radio, the
  station name is shown first with the RDS/stream title on the next line as
  `Now playing: …`. A quiet artist note shows genres, albums and track counts
  from your own library; **Fetch bio** is an opt-in Wikipedia lookup whose short
  summary is cached locally under `artist-info.json`. Nothing is fetched until
  you click it. No VU or level panel is shown: the player does not expose a
  usable level feed through the documented control protocol, and HifiMagic will
  not draw a moving needle that is not driven by real samples.
- **Sources** — switch between Internet Radio, Media Library, AirPlay, Spotify,
  Coax/Optical, USB Audio, Google Cast, Roon, TIDAL/Qobuz Connect, etc. Only
  sources the device marks as selectable are shown.
- **Presets** — play, rename and delete saved presets, with search.
- **Play queue** — browse the current queue, jump to a track, add any track to
  your playlist, move tracks up or down, remove one track, save the whole queue
  as a named playlist, or clear the queue (with confirmation). Queue edits are
  checked against a fresh queue listing and hidden when the player has no queue.
- **USB / Library** — browse the attached USB drive folder by folder using the player's local
  UPnP server (port `8050`). Every album and folder has a **⋮** menu with Play now, Play next,
  Add to player queue, and Add to playlist (existing or new). Switch between a compact
  **icons** grid (the default, sized like the radio preset cards) and a detailed **list**
  view from the toggle in the folder toolbar; your choice is remembered. Empty
  artwork/scan folders are hidden only after their contents are checked, so an album
  really called "Artwork" is still shown.
- **Playlists** — static lists and smart searches live under one menu. Build a named,
  re-orderable playlist from USB folders/albums/tracks, queue tracks, presets or stream URLs.
  Save it under any name, then load, delete or re-save it from the dropdown. **Play all**
  queues the playable items in order and starts the first one; it no longer starts every
  track in turn. Saved tracks are re-resolved against the current USB tree before they
  are queued, so a remounted drive with new resource paths still plays. Saving a smart
  search with an existing name asks whether to merge (keep
  the currently visible tracks and add the new matches) or overwrite. Playlists are stored
  on this computer, not in a browser profile or in the cloud.
- **Favourites** — star any album or track in the USB library, search results,
  queue or working playlist. Saved items live in `favourites.json`, can be played
  or added to the working playlist, and are included in backup. Radio stations
  have their own saved-station list.
- **Discover** — a local, searchable index of the USB library. Type
  `christmas songs`, `artist:Enya`, `"jingle bells"` or `genre:jazz`, then play a
  result or add the whole set to the current playlist. Save a search as a
  **smart playlist** to have it refresh automatically after a library re-scan.
  Search and smart playlists work entirely offline. Optionally, an
  OpenAI-compatible endpoint can interpret natural-language requests; it is
  never contacted unless you supply a key. **Deep scan for broken entries**
  re-browses the USB tree read-only and reports missing object IDs, resource
  paths changed by a USB remount, tracks with no duration, and folders the
  player could not read. It checks library integrity, not audio bytes — true
  file-content verification is not exposed by the player's read-only API.
  **Fetch album info** looks up release facts from the selected music database;
  nothing is contacted until that button is pressed.
- **Internet Radio** — browse a community station directory by name, country,
  genre, language and minimum bitrate, then play a station or save it locally.
  The directory is contacted only when you search; results are cached under
  `radio-cache.json` and saved stations under `radio-favourites.json`. No account
  or sign-in is involved. You can still paste a stream URL directly or add it to
  a playlist; stored device presets remain managed under Presets.
- **Settings** — split into **Player settings** and **App settings**, with a
  **Show advanced settings** button that is off by default. Player settings
  include volume/pre-amp/control bus, volume limit, display brightness, standby
  mode, auto power-down, the player's native sleep timer, and name. Advanced
  player settings add 7-band EQ, room correction, audio output, firmware
  status/check, early-update channel and network details; controls the player
  does not report are hidden. App settings hold the album metadata source, USB
  library layout and **Local data backup**. The backup downloads all known
  settings, playlists, favourites, the library index, artist and album notes,
  and radio data as a zip, then restores only validated files from the same zip.
  The archive is never uploaded anywhere.
- **Power awareness** — the header distinguishes **Connected · On** from
  **Connected · Standby**. Browsing the library, changing source, playing music,
  or using another control wakes the player automatically (with Network standby).
  When the player wakes, cached USB browse data is dropped and the library is
  re-read, so a drive attached or remounted while asleep reappears. A
  **Refresh USB** button in the library toolbar forces the same rescan at any time.
- **Live updates** — the UI follows the player in real time via Server-Sent
  Events, including changes made with the physical remote or the official app.
- **Command palette** — press `Ctrl+K` / `Cmd+K` or the ⌕ button to search the
  local library and run commands without leaving the keyboard.
- **Mini player** — `Ctrl+M` / `Cmd+M` opens a real, small browser window with just
  the art, title and transport controls, so it can be left on top or minimized.
  `Ctrl+Shift+M` toggles the in-page compact dock. Each tab, including the mini
  player, sends its own heartbeat. Closing the last browser window stops the local
  controller within a few seconds; other open tabs keep it running. If a browser
  crashes before it can send a close signal, a watchdog exits after 90 seconds of
  missed heartbeats. HifiMagic never starts automatically when no browser has
  opened it.

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
startup only reads state. Automatic discovery only sends an HTTP `HEAD` request
to local subnet addresses. Shutdown is accepted only from `127.0.0.1` or `::1`.
Custom metadata URLs are validated server-side: http(s) only, no embedded
credentials or fragments, only `{artist}` and `{album}` placeholders, and a
bounded JSON response with a small allowlist of fields. Artist bios and album
lookups happen only on explicit button presses.

## Where playlists and settings live

HifiMagic writes no user data to the cloud and does not use your browser profile
as the source of truth. The local server stores:

| Platform | Directory |
| --- | --- |
| Linux | `~/.local/share/hifimagic/` (or `$XDG_DATA_HOME/hifimagic/`) |
| macOS | `~/Library/Application Support/HifiMagic/` |

Files in that directory:

- `config.json` — the last player IP, port and friendly name.
- `playlists.json` — named static and smart playlists.
- `favourites.json` — starred albums and tracks.
- `library-index.json` — metadata from the read-only USB library scan.
- `artist-info.json` — artist bios you chose to fetch and cached locally.
- `album-info.json` — album metadata source, custom URL template and cached
  album facts.
- `radio-favourites.json` / `radio-cache.json` — saved stations and search cache.
- `ui-settings.json` — workspace preferences included in backup, including the
  advanced-settings choice.

The browser only caches the playlist you are currently editing; the server files
are the source of truth, so saved playlists and the device address survive a
browser reset. **Settings → Local data backup → Download backup** saves these
files, plus browser view preferences and the working playlist, as a zip.
**Restore from zip** checks the manifest hashes and only replaces known JSON
files; it never writes outside the data directory.

Optional AI query interpretation is disabled by default. To enable it, set
`HIFIMAGIC_AI_KEY` before starting the app, and optionally
`HIFIMAGIC_AI_URL` and `HIFIMAGIC_AI_MODEL` for an OpenAI-compatible service.
Without a key, all search stays local.

## Run from source (Linux or macOS)

Requires Python 3.9 or newer (no pip, no third-party modules):

```bash
python3 hifimagic.py
```

Then open <http://127.0.0.1:8765>. Useful options:

```bash
python3 hifimagic.py --host 192.168.1.20 --port 8765 --no-browser
python3 hifimagic.py --bind 0.0.0.0   # expose the UI to other machines on your LAN
python3 hifimagic.py --verbose        # log protocol traffic details
```

`python3 cxn100.py` remains as a compatibility shim for older shortcuts.

If the chosen local port is already in use, HifiMagic tries the next 19 ports
and prints the URL it selected.

To show a small, unobtrusive credit-card icon + **Support HiFiMagic** link in the
footer, set an http(s) URL. Clicking it opens the payment page with €10 prefilled;
the amount can be changed there. There are no amount controls in the app:

```bash
HIFIMAGIC_SUPPORT_URL=https://your-payment-link.example/iris3th python3 hifimagic.py
# or
python3 hifimagic.py --support-url https://your-payment-link.example/iris3th
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
customer-chosen EUR amount (preset €10, minimum €1, maximum €50). It is one-time
only, needs no webhook or backend, and keeps your secret key out of the
distributed app. The publishable key is not needed for a hosted redirect. The
footer link automatically adds Stripe's `prefilled_amount=1000`, so the payment
page opens with €10 selected even for older links; the visitor can edit the
amount (for example to €5) on Stripe's page.

A test link is available for verification:

```text
https://buy.stripe.com/test_6oU4gtaH1grI4FJbVv1B600
```

Test it with:

```bash
HIFIMAGIC_SUPPORT_URL=https://buy.stripe.com/test_6oU4gtaH1grI4FJbVv1B600 python3 hifimagic.py
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

If the player is in Eco standby mode its network interface is off, so HifiMagic
cannot wake it or see the USB drive until you press the button on the device. Set
**Standby Mode → Network standby** on the device (or in this app while it is
awake) to let the app wake it and re-scan USB when you browse or play.

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

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local HTTP/SSE bridge between the browser UI and the CXN100.

Every command the browser can send goes through an explicit, validated
allow-list here. Arbitrary SMOIP paths from a browser are never forwarded, so
the player cannot be misconfigured or factory-reset from the web UI.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen

import backup
import discovery
import library
import storage
import upnp
from smoip import SMOIPClient, SMOIPError

LOG = logging.getLogger("cxn100.server")
ZONE = "ZONE1"
WEB_ROOT = Path(__file__).resolve().parent / "web"
DEFAULT_DEVICE_PORT = 80
DEFAULT_UPNP_PORT = 8050
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
try:
    _CLIENT_TTL = max(20.0, float(os.environ.get("HIFIMAGIC_CLIENT_TTL", "90")))
except ValueError:
    _CLIENT_TTL = 90.0
_SHUTDOWN_DELAY = 3.0


def _clean_support_url(value: Any) -> str:
    """Only allow ordinary http(s) links in the footer."""
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return text


METER_AUDIO_EXTENSIONS = {".flac", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus"}
METER_AUDIO_MIME = {
    ".flac": "audio/flac", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".ogg": "audio/ogg", ".opus": "audio/ogg",
}
METER_MAX_FILE_BYTES = 300 * 1024 * 1024
METER_MAX_FILES = 500
LOOPBACK_ADDRESSES = {"127.0.0.1", "::1"}


def _meter_root(folder: str) -> Path | None:
    """Resolve a configured meter folder without following it outside itself."""
    text = str(folder or "").strip()
    if not text:
        return None
    try:
        root = Path(text).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if not root.is_dir():
        return None
    return root


def _meter_files(folder: str) -> tuple[list[dict[str, Any]], str]:
    """List audio files directly inside the configured folder, read-only."""
    root = _meter_root(folder)
    if root is None:
        return [], "Folder not found"
    files: list[dict[str, Any]] = []
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        return [], str(exc)
    for entry in entries:
        if entry.suffix.casefold() not in METER_AUDIO_EXTENSIONS:
            continue
        try:
            resolved = entry.resolve()
            if resolved.parent != root or not resolved.is_file():
                continue
            stat = resolved.stat()
        except OSError:
            continue
        files.append({"name": entry.name, "size": stat.st_size, "mtime": stat.st_mtime})
        if len(files) >= METER_MAX_FILES:
            break
    return files, ""


def clean_meter_folder(value: Any) -> str:
    """Validate a user-supplied folder for local peak-meter analysis."""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 4096 or "\x00" in text:
        raise ValueError("Audio folder path is invalid")
    root = _meter_root(text)
    if root is None:
        raise ValueError("Audio folder not found")
    if root == Path("/") or root == Path.home().resolve():
        raise ValueError("Choose a specific music folder, not the whole drive or home")
    return str(root)


def _media_text(value: Any) -> str:
    """Normalise metadata for tolerant comparisons after a USB remount."""
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _media_text_matches(saved: Any, live: Any) -> bool:
    left = _media_text(saved)
    right = _media_text(live)
    if not left or not right:
        return True
    if left == right:
        return True
    if len(left) >= 8 and (left.startswith(right) or right.startswith(left)):
        return True
    left_words = set(left.split())
    right_words = set(right.split())
    if len(left_words) >= 2 and len(right_words) >= 2:
        shared = len(left_words & right_words) / min(len(left_words), len(right_words))
        return shared >= 0.8
    return False


def _saved_item_matches(saved: dict[str, Any], live: dict[str, Any]) -> bool:
    # A remount changes the resource path, so metadata is the reliable signal.
    # Require at least one shared field and reject any field that disagrees.
    matched = 0
    for field in ("title", "artist", "album"):
        wanted = saved.get(field)
        current = live.get(field)
        if wanted and current:
            if not _media_text_matches(wanted, current):
                return False
            matched += 1
    return matched > 0


# Paths fetched on every refresh. All are read-only.
POLL_PATHS: list[tuple[str, str, dict[str, Any]]] = [
    ("device", "/system/info", {}),
    ("sources", "/system/sources", {}),
    ("zone", "/zone/state", {"zone": ZONE}),
    ("playback", "/zone/play_state", {"zone": ZONE}),
    ("now_playing", "/zone/now_playing", {"zone": ZONE}),
    ("queue", "/queue/list", {}),
    ("presets", "/presets/list", {}),
    ("audio", "/zone/audio", {"zone": ZONE}),
    ("outputs", "/zone/audio/output", {"zone": ZONE}),
    ("display", "/system/display", {}),
    ("power", "/system/power", {}),
    ("update", "/system/update", {}),
    ("network", "/system/network", {}),
    ("upnp", "/system/upnp", {}),
]

SPEC_PATHS: list[tuple[str, str]] = [
    ("zone", "/zone/state/spec"),
    ("audio", "/zone/audio/spec"),
    ("play_control", "/zone/play_control/spec"),
    ("power", "/system/power/spec"),
    ("presets", "/presets/spec"),
    ("display", "/system/display/spec"),
]

EQ_PRESETS: dict[str, list[float]] = {
    "flat": [0, 0, 0, 0, 0, 0, 0],
    "bass_boost": [3, 3, 1, 0, -1, -0.5, -0.3],
    "bass_reduction": [-4.6, -1.8, -0.6, 0, 0.6, 0.4, 0],
    "voice_clarity": [-6, -3.4, 3, 3, 3, 2.2, -1.4],
    "treble_boost": [0, 0, 0, 0, 0.6, 1.8, 3],
    "treble_reduction": [0, 0, 0, 0, 0, -1.2, -4.2],
    "tv": [-1.9, -0.8, 1, 1, 0.8, 0, -0.8],
    "movie": [0, 1.4, -0.4, -2, -0.6, 0.6, 1.1],
    "gaming": [3, 3, 1, -1, -1, 0.6, -0.2],
}

EQ_FILTERS = {
    "PASSTHROUGH", "PEAKING", "LOWSHELF", "HIGHSHELF",
    "NOTCH", "HIGHPASS", "LOWPASS", "ALLPASS",
}


def _client_id(value: Any) -> str:
    text = str(value or "").strip()
    return text if _CLIENT_ID_RE.fullmatch(text) else ""


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "1", "on", "yes"}:
        return True
    if isinstance(value, str) and value.lower() in {"false", "0", "off", "no"}:
        return False
    raise SMOIPError(f"Expected a boolean, got {value!r}")


def _as_int(value: Any, name: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise SMOIPError(f"{name} must be a number") from exc
    if not low <= number <= high:
        raise SMOIPError(f"{name} must be between {low} and {high}")
    return number


def _as_enum(value: Any, name: str, allowed: set[str]) -> str:
    text = str(value)
    if text not in allowed:
        raise SMOIPError(f"Invalid {name}: {text!r} (expected {', '.join(sorted(allowed))})")
    return text


def _as_text(value: Any, name: str, maximum: int = 128) -> str:
    text = str(value).strip()
    if not text or len(text) > maximum:
        raise SMOIPError(f"{name} must be 1-{maximum} characters")
    return text


def _player_is_on(snapshot: dict[str, Any]) -> bool:
    """Treat the explicit power path as authoritative; fall back to zone state."""
    power = snapshot.get("power") or {}
    if isinstance(power, dict) and "power" in power:
        return str(power.get("power") or "").upper() == "ON"
    zone = snapshot.get("zone") or {}
    return isinstance(zone, dict) and zone.get("power") is True


def _playlist_media_item(value: Any) -> dict[str, Any]:
    """Validate a library item that is about to be stored or queued."""
    if not isinstance(value, dict):
        raise SMOIPError("No library item supplied")
    object_id = _as_text(value.get("id"), "library item id", 160)
    if not re.fullmatch(r"[A-Za-z0-9_:.\-]{1,160}", object_id):
        raise SMOIPError("Invalid library object id")
    class_name = _as_text(value.get("class"), "library item class", 120)
    if not (class_name.startswith("object.item.audioItem") or
            class_name.startswith("object.container")):
        raise SMOIPError("Only USB library items can be stored in a playlist")
    result: dict[str, Any] = {"id": object_id, "class": class_name}
    fields = {
        "parent_id": 160, "title": 300, "artist": 300, "album": 300,
        "genre": 160, "track": 40, "duration": 40, "resource": 2048,
        "art_url": 2048, "path": 600, "didl": 16384,
    }
    for field, maximum in fields.items():
        text = str(value.get(field) or "").strip()
        if len(text) > maximum:
            raise SMOIPError(f"{field} is too long")
        result[field] = text
    result["is_container"] = bool(value.get("is_container") or
                                  class_name.startswith("object.container"))
    return result


def _unique_playlist_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove duplicate tracks while preserving the caller's order."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in items:
        key = str(item.get("id") or item.get("key") or item.get("title") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        unique.append(item)
    return unique


def _eq_string(bands: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for band in bands:
        index = _as_int(band.get("index"), "band index", 0, 6)
        filter_type = _as_enum(band.get("filter", "PEAKING"), "filter", EQ_FILTERS)
        freq = _as_int(band.get("freq", 1000), "frequency", 20, 20000)
        gain = int(_as_int(band.get("gain", 0), "gain", -60, 30)) / 10.0
        q = int(_as_int(band.get("q", 124), "Q", 10, 100)) / 100.0
        parts.append(f"{index},{filter_type},{freq},{gain:.1f},{q:.2f}")
    return "|".join(parts)


class MediaLibrary:
    """Read-only USB browsing plus validated, DIDL-based queue commands."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.cache: dict[tuple[str, str], dict[str, Any]] = {}
        self.hidden_counts: dict[tuple[str, str], int] = {}
        self.mount_cache: tuple[float, str] | None = None
        self.primed_paths: set[tuple[str, str]] = set()

    def reset(self) -> None:
        """Forget browse metadata so a waking/remounted USB tree is re-read."""
        with self.lock:
            self.cache.clear()
            self.hidden_counts.clear()
            self.mount_cache = None
            self.primed_paths.clear()

    @staticmethod
    def _server_udn(state: "PlayerState") -> str:
        devices = (state.state().get("upnp") or {}).get("devices") or []
        for device in devices:
            udn = str(device.get("udn") or "").strip()
            if udn:
                return udn
            description = str(device.get("description_url") or "")
            match = re.search(r"/([0-9A-Fa-f-]{36})/description\.xml", description)
            if match:
                return match.group(1)
        raise SMOIPError("The player's UPnP media server is not available yet")

    @classmethod
    def _is_empty_artwork_folder(
        cls, state: "PlayerState", udn: str, item: dict[str, Any]
    ) -> bool:
        return upnp.is_empty_artwork_folder(
            state.device, udn, item, port=DEFAULT_UPNP_PORT
        )

    def browse(
        self,
        state: "PlayerState",
        object_id: str = "0",
        start: int = 0,
        count: int = 100,
    ) -> dict[str, Any]:
        object_id = _as_text(object_id, "object id", 160)
        if not re.fullmatch(r"[A-Za-z0-9_:.\-]+", object_id):
            raise SMOIPError("Invalid library object id")
        udn = self._server_udn(state)
        result = upnp.browse(
            state.device,
            udn,
            object_id=object_id,
            start=start,
            count=count,
            port=DEFAULT_UPNP_PORT,
        )
        items: list[dict[str, Any]] = []
        key = (udn, object_id)
        raw_total = int(result["total"])
        number_returned = int(result.get("number_returned") or len(result["items"]))
        with self.lock:
            if start == 0:
                self.hidden_counts[key] = 0
            for item in result["items"]:
                if self._is_empty_artwork_folder(state, udn, item):
                    self.hidden_counts[key] = self.hidden_counts.get(key, 0) + 1
                    continue
                try:
                    clean = upnp.normalize_item(item)
                except upnp.UPnPError:
                    continue
                clean["is_container"] = bool(item.get("is_container"))
                clean["restricted"] = str(item.get("restricted") or "")
                didl = str(item.get("didl") or upnp.media_didl(clean))
                clean["didl"] = didl
                self.cache[(udn, clean["id"])] = clean
                items.append(clean)
            total = max(0, raw_total - self.hidden_counts.get(key, 0))
            if len(self.cache) > 2000:
                self.cache.clear()
                self.hidden_counts.clear()
        return {
            "object_id": object_id,
            "items": items,
            "total": total,
            "start": start,
            "count": len(items),
            "has_more": start + number_returned < raw_total,
            "next_start": start + number_returned,
            "server_udn": udn,
        }

    def _prime_parent_path(
        self, state: "PlayerState", udn: str, parent_id: str
    ) -> bool:
        """Browse each ancestor before the target folder.

        This firmware only populates a ContentDirectory branch when its parent
        has been browsed first. Jumping straight to ``<prefix>:0_1_0`` returns
        HTTP 500 forever after a wake, while browsing ``<prefix>:0`` and
        ``<prefix>:0_1`` first makes it available immediately.
        """
        if ":" not in parent_id:
            return False
        prefix, tail = parent_id.split(":", 1)
        parts = [part for part in tail.split("_") if part]
        if not parts:
            return False
        path: list[str] = []
        for part in parts:
            path.append(part)
            object_id = f"{prefix}:{'_'.join(path)}"
            key = (udn, object_id)
            with self.lock:
                if key in self.primed_paths:
                    continue
            try:
                upnp.browse(
                    state.device, udn, object_id=object_id, start=0, count=50,
                    port=DEFAULT_UPNP_PORT,
                )
            except (upnp.UPnPError, OSError):
                return False
            with self.lock:
                self.primed_paths.add(key)
        return True

    def _current_mount_number(self, state: "PlayerState", udn: str) -> str | None:
        """Read the active ``/tmp/usm/<n>`` mount from any playable track."""
        now = time.monotonic()
        with self.lock:
            cached = self.mount_cache
        if cached and now - cached[0] < 30:
            return cached[1]

        def scan(items: list[dict[str, Any]]) -> str | None:
            for entry in items:
                found = re.search(r"/tmp/usm/(\d+)/", str(entry.get("resource") or ""))
                if found:
                    return found.group(1)
                if entry.get("is_container"):
                    try:
                        children = upnp.browse(
                            state.device, udn,
                            object_id=str(entry.get("id") or ""), start=0, count=10,
                            port=DEFAULT_UPNP_PORT,
                        )
                    except (upnp.UPnPError, OSError):
                        continue
                    found_mount = scan(children.get("items") or [])
                    if found_mount:
                        return found_mount
            return None

        try:
            roots = upnp.browse(
                state.device, udn, object_id="0", start=0, count=10,
                port=DEFAULT_UPNP_PORT,
            )
        except (upnp.UPnPError, OSError):
            return cached[1] if cached else None
        mount = scan(roots.get("items") or [])
        if mount:
            self.mount_cache = (now, mount)
            return mount
        return cached[1] if cached else None

    def _rewrite_saved_item(
        self, item: dict[str, Any], state: "PlayerState", udn: str,
        prefixes: list[str],
    ) -> dict[str, Any] | None:
        """Best-effort DIDL rebuild when a live parent still has no match.

        Normally ``resolve()`` finds the track in the live tree after walking
        the ancestor folders. This last resort uses the saved relative path
        with the current object-id prefix and mount number; live metadata is
        always preferred because the player validates DIDL strictly.
        """
        didl = str(item.get("didl") or "")
        if not didl.startswith("<DIDL-Lite") or not re.search(r"/tmp/usm/(\d+)/", didl):
            return None
        object_id = str(item.get("id") or "").strip()
        parent_id = str(item.get("parent_id") or "").strip()
        if ":" not in object_id or (parent_id and ":" not in parent_id):
            return None
        if not prefixes:
            try:
                roots = upnp.browse(
                    state.device, udn, object_id="0", start=0, count=200,
                    port=DEFAULT_UPNP_PORT,
                )
            except (upnp.UPnPError, OSError):
                return None
            prefixes = []
            for entry in roots.get("items") or []:
                found = re.match(r"^([A-Za-z0-9_-]+):", str(entry.get("id") or ""))
                if found and found.group(1) not in prefixes:
                    prefixes.append(found.group(1))
        if not prefixes:
            return None
        mount = self._current_mount_number(state, udn)
        if not mount:
            return None

        def remap(value: str) -> str:
            if ":" not in value:
                return value
            return f"{prefixes[0]}:{value.split(':', 1)[1]}"

        new_id = remap(object_id)
        new_parent = remap(parent_id) if parent_id else ""
        rewritten = didl.replace(f"'{object_id}'", f"'{new_id}'")
        if parent_id:
            rewritten = rewritten.replace(f"'{parent_id}'", f"'{new_parent}'")
        rewritten = re.sub(r"/tmp/usm/\d+/", f"/tmp/usm/{mount}/", rewritten)
        found = re.search(r"<res[^>]*>(.*?)</res>", rewritten, re.S)
        resource = found.group(1).strip() if found else re.sub(
            r"/tmp/usm/\d+/", f"/tmp/usm/{mount}/", str(item.get("resource") or "")
        )
        result = dict(item)
        result.update({
            "id": new_id,
            "parent_id": new_parent,
            "didl": rewritten,
            "resource": resource,
        })
        if result.get("path"):
            result["path"] = re.sub(
                r"/tmp/usm/\d+/", f"/tmp/usm/{mount}/", str(result["path"])
            )
        with self.lock:
            self.cache[(udn, new_id)] = result
        return result

    def resolve(self, state: "PlayerState", item: Any) -> dict[str, Any] | None:
        """Find current UPnP metadata for a saved item after a USB remount.

        A remount can change both the ``/tmp/usm`` path and the ContentDirectory
        object-id prefix (for example ``1:0_...`` becomes ``3:0_...``). Walk the
        current branch from its root down to the saved parent, then match the
        track by id and metadata. Checking metadata prevents a changed suffix
        from silently queueing the wrong track.
        """
        if not isinstance(item, dict):
            return None
        object_id = str(item.get("id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_:.\-]{1,160}", object_id):
            return None
        udn = self._server_udn(state)
        prefixes: list[str] = []
        try:
            roots = upnp.browse(
                state.device, udn, object_id="0", start=0, count=200,
                port=DEFAULT_UPNP_PORT,
            )
            for entry in roots.get("items") or []:
                found = re.match(r"^([A-Za-z0-9_-]+):", str(entry.get("id") or ""))
                if found and found.group(1) not in prefixes:
                    prefixes.append(found.group(1))
        except (upnp.UPnPError, OSError, SMOIPError):
            pass

        def keep(entry: dict[str, Any], exact_id: bool) -> dict[str, Any] | None:
            if not str(entry.get("didl") or "").startswith("<DIDL-Lite"):
                return None
            if not exact_id and not _saved_item_matches(item, entry):
                return None
            try:
                clean = upnp.normalize_item(entry)
            except upnp.UPnPError:
                return None
            clean["is_container"] = bool(entry.get("is_container"))
            clean["restricted"] = str(entry.get("restricted") or "")
            clean["didl"] = str(entry["didl"])
            with self.lock:
                self.cache[(udn, clean["id"])] = clean
            return clean

        def live_ids(value: str) -> list[str]:
            """Map a saved id to the same path below each current root prefix."""
            if ":" not in value or not prefixes:
                return [value]
            tail = value.split(":", 1)[1]
            return list(dict.fromkeys(f"{prefix}:{tail}" for prefix in prefixes))

        candidates = live_ids(object_id)
        parent_id = str(item.get("parent_id") or "").strip()
        parents = (
            live_ids(parent_id)
            if re.fullmatch(r"[A-Za-z0-9_:.\-]{1,160}", parent_id)
            else []
        )
        # Browse each ancestor first. Skipping straight to a deep folder makes
        # this firmware answer HTTP 500 indefinitely after a USB remount.
        for parent in dict.fromkeys(parents):
            self._prime_parent_path(state, udn, parent)
            try:
                result = upnp.browse(
                    state.device, udn, object_id=parent, start=0, count=500,
                    port=DEFAULT_UPNP_PORT,
                )
            except (upnp.UPnPError, OSError, SMOIPError) as exc:
                LOG.debug("Resolve parent %s failed: %s", parent, exc)
                continue
            for entry in result.get("items") or []:
                entry_id = str(entry.get("id") or "")
                if entry_id not in candidates and not _saved_item_matches(item, entry):
                    continue
                resolved = keep(entry, entry_id in candidates)
                if resolved is not None:
                    LOG.debug("Resolved saved item %s to %s in %s", object_id, entry_id, parent)
                    return resolved
        rewritten = self._rewrite_saved_item(item, state, udn, prefixes)
        if rewritten is not None:
            LOG.debug("Rebuilt saved item %s for remount as %s", object_id, rewritten["id"])
            return rewritten
        LOG.debug("Could not resolve saved item %s in the live USB tree", object_id)
        return None

    def add(self, state: "PlayerState", item: Any, action: str = "APPEND") -> dict[str, Any]:
        if not isinstance(item, dict):
            raise SMOIPError("No library item supplied")
        action = _as_enum(action, "action", {"APPEND", "PLAY_NOW", "PLAY_NEXT"})
        object_id = str(item.get("id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_:.\-]{1,160}", object_id):
            raise SMOIPError("Invalid library object id")
        udn = self._server_udn(state)
        with self.lock:
            cached = self.cache.get((udn, object_id))
        supplied_didl = str(item.get("didl") or "")
        if supplied_didl.startswith("<DIDL-Lite"):
            # A freshly resolved library item carries current DIDL. Prefer it
            # over an older cache entry from a previous USB mount.
            didl = supplied_didl
        elif cached:
            didl = str(cached.get("didl") or upnp.media_didl(cached))
        else:
            clean = upnp.normalize_item(item)
            didl = upnp.media_didl(clean)
        try:
            return upnp.queue_add(state.device, state.device_port, udn, didl, action)
        except upnp.UPnPError as original_error:
            # The player can remount the USB drive after the index was built,
            # changing both resource URLs and ContentDirectory IDs. Find the
            # same track in the live tree before falling back to older probes.
            refreshed: dict[str, Any] | None = None
            try:
                refreshed = self.resolve(state, item)
            except (upnp.UPnPError, OSError, SMOIPError):
                pass
            if refreshed is not None:
                didl = str(refreshed["didl"])
                return upnp.queue_add(state.device, state.device_port, udn, didl, action)
            # Some firmware versions reject BrowseMetadata on tracks, so also
            # try the saved object and parent ids directly.
            try:
                fresh = upnp.browse(
                    state.device,
                    udn,
                    object_id=object_id,
                    flag="BrowseMetadata",
                    start=0,
                    count=1,
                    port=DEFAULT_UPNP_PORT,
                )
                refreshed = next(
                    (
                        entry for entry in fresh.get("items", [])
                        if str(entry.get("didl") or "").startswith("<DIDL-Lite")
                    ),
                    None,
                )
            except upnp.UPnPError:
                pass
            if refreshed is None:
                parent_id = str(item.get("parent_id") or "").strip()
                if re.fullmatch(r"[A-Za-z0-9_:.\-]{1,160}", parent_id):
                    try:
                        parent = upnp.browse(
                            state.device,
                            udn,
                            object_id=parent_id,
                            start=0,
                            count=500,
                            port=DEFAULT_UPNP_PORT,
                        )
                    except upnp.UPnPError:
                        parent = {"items": []}
                    refreshed = next(
                        (
                            entry for entry in parent.get("items", [])
                            if str(entry.get("id")) == object_id
                            and str(entry.get("didl") or "").startswith("<DIDL-Lite")
                        ),
                        None,
                    )
                    if refreshed is None:
                        refreshed = next(
                            (
                                entry for entry in parent.get("items", [])
                                if str(entry.get("title") or "").casefold()
                                == str(item.get("title") or "").casefold()
                                and str(entry.get("didl") or "").startswith("<DIDL-Lite")
                            ),
                            None,
                        )
            if refreshed is None:
                raise original_error
            didl = str(refreshed["didl"])
            with self.lock:
                self.cache[(udn, str(refreshed.get("id") or object_id))] = refreshed
            return upnp.queue_add(state.device, state.device_port, udn, didl, action)


class PlayerState:
    """Keeps a snapshot of the player and pushes updates to SSE clients."""

    def __init__(
        self,
        client: SMOIPClient,
        device: str,
        device_port: int = DEFAULT_DEVICE_PORT,
        support_url: str = "",
        config_store: storage.ConfigStore | None = None,
    ) -> None:
        self.client = client
        self.device = device
        self.device_port = device_port
        self.support_url = _clean_support_url(support_url)
        self.config_store = config_store
        self.lock = threading.RLock()
        self._subscribed_client: SMOIPClient | None = None
        self.snapshot: dict[str, Any] = {
            "connected": False,
            "connecting": True,
            "last_error": "",
            "device_address": device,
            "device_port": device_port,
            "revision": 0,
            "device": {},
            "sources": [],
            "zone": {},
            "playback": {},
            "now_playing": {},
            "queue": {},
            "presets": {},
            "audio": {},
            "outputs": {},
            "display": {},
            "power": {},
            "update": {},
            "network": {},
            "upnp": {},
            "specs": {},
        }
        self.listeners: list[queue.Queue[dict[str, Any]]] = []
        self._stop = threading.Event()
        self.on_wake: Callable[[], None] | None = None
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True, name="state-poll")

    def start(self) -> None:
        self._poll_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.client.close()

    def add_listener(self) -> queue.Queue[dict[str, Any]]:
        listener: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=32)
        with self.lock:
            self.listeners.append(listener)
        return listener

    def remove_listener(self, listener: queue.Queue[dict[str, Any]]) -> None:
        with self.lock:
            if listener in self.listeners:
                self.listeners.remove(listener)

    def _reset_player_data(self) -> None:
        for key in ("device", "zone", "playback", "now_playing", "queue", "presets",
                    "audio", "outputs", "display", "power", "update", "network", "upnp", "specs"):
            self.snapshot[key] = {}
        self.snapshot["sources"] = []

    def _remember_device(self) -> None:
        if self.config_store is None:
            return
        try:
            self.config_store.save(
                self.device,
                self.device_port,
                str((self.snapshot.get("device") or {}).get("name") or ""),
            )
        except (storage.StorageError, OSError) as exc:
            LOG.warning("Could not save device config: %s", exc)

    def config(self) -> dict[str, Any]:
        with self.lock:
            return {
                "host": self.device,
                "port": self.device_port,
                "connected": bool(self.snapshot["connected"]),
                "connecting": bool(self.snapshot["connecting"]),
                "last_error": self.snapshot["last_error"],
                "support_url": self.support_url,
                "meter_folder": self.meter_folder(),
            }

    def meter_folder(self) -> str:
        if self.config_store is None:
            return ""
        try:
            return str(self.config_store.load().get("meter_folder") or "")
        except OSError:
            return ""

    def set_meter_folder(self, folder: Any) -> dict[str, Any]:
        if self.config_store is None:
            raise storage.StorageError("Local settings are unavailable")
        self.config_store.set_meter_folder(clean_meter_folder(folder))
        return self.config()

    def configure(self, host: Any, port: Any) -> dict[str, Any]:
        """Switch the player connection. The web server never goes down."""
        host = _as_text(host, "IP address", 253)
        if not _HOST_RE.fullmatch(host):
            raise SMOIPError("IP address may contain only letters, numbers, dots, '-' and '_'")
        port = _as_int(port, "port", 1, 65535)

        with self.lock:
            if host == self.device and port == self.device_port and self.client.connected:
                self.snapshot["connecting"] = False
                self._remember_device()
                return self.config()
            old = self.client
            client = SMOIPClient(host, port, timeout=8.0)
            client.on_connection = self.on_connection
            client.on_message = self.on_message
            self.device = host
            self.device_port = port
            self.client = client
            self._subscribed_client = None
            self.snapshot.update({
                "connected": False,
                "connecting": True,
                "last_error": "",
                "device_address": host,
                "device_port": port,
            })
            self._reset_player_data()

        old.close()
        try:
            client.connect()
        except (OSError, SMOIPError) as exc:
            with self.lock:
                self.snapshot["connecting"] = False
                self.snapshot["connected"] = False
                self.snapshot["last_error"] = str(exc)
            self.publish()
            raise SMOIPError(f"Cannot reach {host}:{port} — {exc}") from exc

        with self.lock:
            self.snapshot["connecting"] = False
            self.snapshot["last_error"] = ""
        self._remember_device()
        self.refresh()
        self.publish()
        return self.config()

    def publish(self) -> None:
        with self.lock:
            self.snapshot["revision"] += 1
            event = {
                "event": "state",
                "revision": self.snapshot["revision"],
                "connected": self.snapshot["connected"],
            }
            listeners = list(self.listeners)
        for listener in listeners:
            try:
                listener.put_nowait(event)
            except queue.Full:
                pass

    def on_connection(self, up: bool) -> None:
        with self.lock:
            self.snapshot["connected"] = up
            if up:
                self.snapshot["last_error"] = ""
        self.publish()

    def on_message(self, path: str, message: dict[str, Any]) -> None:
        """Apply unsolicited device updates (subscriptions)."""
        changed = self._apply(path, message.get("params", {}))
        if changed:
            self.publish()

    def _apply(self, path: str, params: dict[str, Any]) -> bool:
        key = next((name for name, p, _ in POLL_PATHS if p == path), None)
        if key is None or not isinstance(params, dict):
            return False
        data = params.get("data", params)
        if not isinstance(data, dict):
            return False
        with self.lock:
            if self.snapshot[key] == data:
                return False
            was_on = _player_is_on(self.snapshot)
            self.snapshot[key] = data
            waking = _player_is_on(self.snapshot) and not was_on
        if waking and self.on_wake:
            try:
                self.on_wake()
            except Exception:
                LOG.debug("Wake refresh failed", exc_info=True)
        return True

    def refresh(self) -> None:
        client = self.client
        client.resubscribe_if_needed()
        changed = False
        errors: list[str] = []
        for key, path, params in POLL_PATHS:
            try:
                response = client.request(path, params, retry=False)
            except SMOIPError as exc:
                LOG.debug("Refresh %s failed: %s", path, exc)
                errors.append(str(exc))
                continue
            changed = self._apply(path, response.get("params", {})) or changed
        with self.lock:
            self.snapshot["last_error"] = errors[-1] if errors else ""
        if changed:
            self.publish()

    def _prepare_client(self, client: SMOIPClient) -> None:
        """Cache capabilities and subscribe once per client connection."""
        self._subscribed_client = client
        for key, path in SPEC_PATHS:
            try:
                response = client.request(path, {}, retry=False)
                data = response.get("params", {}).get("data", {})
                if isinstance(data, dict):
                    with self.lock:
                        self.snapshot["specs"][key] = data
            except SMOIPError as exc:
                LOG.debug("Spec %s unavailable: %s", path, exc)
        try:
            client.subscribe([path for _, path, _ in POLL_PATHS])
        except SMOIPError as exc:
            LOG.debug("Subscription setup failed: %s", exc)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            client = self.client
            try:
                if client is not self._subscribed_client:
                    self._prepare_client(client)
                if not client.connected:
                    client.ensure_connected()
                self.refresh()
                with self.lock:
                    self.snapshot["connecting"] = False
            except (OSError, SMOIPError) as exc:
                LOG.debug("Poll failed: %s", exc)
                with self.lock:
                    self.snapshot["connecting"] = False
                    self.snapshot["last_error"] = str(exc)
                self.publish()
            self._stop.wait(2.5)

    def is_on(self) -> bool:
        with self.lock:
            return _player_is_on(self.snapshot)

    def state(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.snapshot))


class ActionHandler:
    """The only bridge from HTTP requests to device commands."""

    # These settings must remain available while the player is in standby.
    NO_WAKE_ACTIONS = {"power_on", "standby", "standby_mode", "auto_power_down"}

    def __init__(self, state: PlayerState) -> None:
        self.state = state
        self.media = MediaLibrary()
        self.actions: dict[str, Callable[[dict[str, Any]], tuple[str, dict[str, Any]] | None]] = {
            "power_on": lambda p: ("/system/power", {"power": "ON"}),
            "standby": lambda p: ("/system/power", {"power": "NETWORK"}),
            "standby_mode": lambda p: (
                "/system/power",
                {"standby_mode": _as_enum(p.get("mode"), "mode", {"NETWORK", "ECO_MODE"})},
            ),
            "auto_power_down": lambda p: (
                "/system/power",
                {"auto_power_down": _as_int(p.get("seconds"), "seconds", 0, 7200)},
            ),
            "set_name": lambda p: ("/system/info", {"name": _as_text(p.get("name"), "name", 64)}),
            "display_brightness": lambda p: (
                "/system/display",
                {"brightness": _as_enum(p.get("value"), "brightness", {"off", "dim", "bright"})},
            ),
            "source": self._source,
            "play": self._play("play"),
            "pause": self._play("pause"),
            "toggle": self._play("toggle"),
            "stop": self._play("stop"),
            "next": self._skip(1),
            "previous": self._skip(-1),
            "seek": lambda p: (
                "/zone/play_control",
                {"zone": ZONE, "position": _as_int(p.get("position"), "position", 0, 86400)},
            ),
            "queue_index": lambda p: (
                "/zone/play_control",
                {"zone": ZONE, "queue_index": _as_int(p.get("index"), "index", 0, 10000)},
            ),
            "queue_clear": self._queue_clear,
            "shuffle": lambda p: (
                "/zone/play_control",
                {"zone": ZONE, "match": "none", "mode_shuffle":
                 _as_enum(p.get("value"), "shuffle", {"off", "all", "toggle"})},
            ),
            "repeat": lambda p: (
                "/zone/play_control",
                {"zone": ZONE, "match": "none", "mode_repeat":
                 _as_enum(p.get("value"), "repeat", {"off", "all", "one", "toggle"})},
            ),
            "radio_url": self._radio_url,
            "radio_airable": self._radio_airable,
            "preset": lambda p: (
                "/zone/recall_preset",
                {"zone": ZONE, "preset": _as_int(p.get("preset"), "preset", 1, 99)},
            ),
            "rename_preset": self._rename_preset,
            "delete_preset": lambda p: (
                "/presets/delete",
                {"preset": _as_int(p.get("preset"), "preset", 1, 99)},
            ),
            "volume_set": lambda p: (
                "/zone/state",
                {"zone": ZONE, "volume_percent": _as_int(p.get("value"), "volume", 0, 100)},
            ),
            "volume_step": lambda p: (
                "/zone/state",
                {"zone": ZONE, "volume_step_change": _as_int(p.get("value"), "step", -100, 100)},
            ),
            "mute": lambda p: ("/zone/state", {"zone": ZONE, "mute": _as_bool(p.get("value"))}),
            "pre_amp": lambda p: ("/zone/state", {"pre_amp_mode": _as_bool(p.get("value"))}),
            "control_bus": lambda p: (
                "/zone/state",
                {"cbus": _as_enum(p.get("value"), "control bus", {"off", "amplifier", "receiver"})},
            ),
            "audio_output": lambda p: (
                "/zone/audio/output",
                {"zone": ZONE, "id": _as_text(p.get("id"), "output id", 64)},
            ),
            "eq_enable": lambda p: ("/zone/audio", {"zone": ZONE, "user_eq": _as_bool(p.get("value"))}),
            "eq_bands": lambda p: ("/zone/audio", {"zone": ZONE, "user_eq_bands": _eq_string(p.get("bands", []))}),
            "eq_preset": self._eq_preset,
            "tilt_enable": lambda p: ("/zone/audio", {"zone": ZONE, "tilt_eq": _as_bool(p.get("value"))}),
            "tilt_intensity": lambda p: (
                "/zone/audio",
                {"zone": ZONE, "tilt_intensity": _as_int(p.get("value"), "intensity", -15, 15)},
            ),
            "volume_limit": lambda p: (
                "/zone/audio",
                {"volume_limit_percent": _as_int(p.get("value"), "limit", 1, 100)},
            ),
            "balance": lambda p: (
                "/zone/audio",
                {"zone": ZONE, "balance": _as_int(p.get("value"), "balance", -15, 15)},
            ),
            "early_update": lambda p: (
                "/system/update",
                {"early_update": _as_bool(p.get("value")), "action": "CHECK"},
            ),
            "check_update": lambda p: ("/system/update", {"action": "CHECK"}),
            "media_add": self._media_add,
            "media_play": self._media_play,
            "media_play_next": self._media_play_next,
            "library_refresh": self._library_refresh,
        }

    def wake_if_needed(self) -> bool:
        """Wake the player for a deliberate UI action, but never on polling."""
        if self.state.is_on():
            return False
        try:
            self.state.client.request(
                "/system/power", {"power": "ON"}, timeout=4.0, retry=False
            )
            self.state.refresh()
            # A network wake remounts USB storage under a new object-id prefix
            # and mount number. Drop cached metadata so the next browse re-reads
            # the live tree instead of replaying stale DIDL.
            self.media.reset()
            LOG.info("Woke the player for a user action")
            return True
        except (OSError, SMOIPError) as exc:
            raise SMOIPError(
                "The player is in standby and could not be woken over the network. "
                "Turn it on with its front button, or use Network standby."
            ) from exc

    def _library_refresh(self, params: dict[str, Any]) -> None:
        self.media.reset()
        self.state.refresh()
        return None

    def _queue_clear(self, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Remove every queued track. The device requires an exact count."""
        listing = self.state.client.get("/queue/list")
        total = int((listing.get("data") or {}).get("total") or 0)
        if total <= 0:
            return "/queue/delete", {"start": 0, "count": 0}
        return "/queue/delete", {"start": 0, "count": total}

    @staticmethod
    def _play(action: str) -> Callable[[dict[str, Any]], tuple[str, dict[str, Any]]]:
        return lambda p: (
            "/zone/play_control",
            {"zone": ZONE, "match": "none", "action": action},
        )

    @staticmethod
    def _skip(amount: int) -> Callable[[dict[str, Any]], tuple[str, dict[str, Any]]]:
        return lambda p: (
            "/zone/play_control",
            {"zone": ZONE, "match": "none", "skip_track": amount},
        )

    def _source(self, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        wanted = _as_text(params.get("id"), "source id", 64)
        sources = self.state.state().get("sources", {}).get("sources", [])
        available = {s.get("id") for s in sources if s.get("ui_selectable", False)}
        if wanted not in available:
            raise SMOIPError(f"Source {wanted!r} is not selectable on this device")
        return "/zone/state", {"zone": ZONE, "source": wanted}

    @staticmethod
    def _radio_url(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        url = _as_text(params.get("url"), "url", 512)
        if not url.lower().startswith(("http://", "https://")):
            raise SMOIPError("Radio URL must start with http:// or https://")
        return "/stream/radio", {
            "zone": ZONE,
            "url": url,
            "name": _as_text(params.get("name", "Internet Radio"), "name", 128),
        }

    @staticmethod
    def _radio_airable(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return "/stream/radio", {
            "zone": ZONE,
            "airable_radio_id": _as_int(params.get("id"), "radio id", 1, 2**63 - 1),
            "name": _as_text(params.get("name", "Internet Radio"), "name", 128),
        }

    @staticmethod
    def _rename_preset(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return "/presets/rename", {
            "preset": _as_int(params.get("preset"), "preset", 1, 99),
            "name": _as_text(params.get("name"), "name", 128),
        }

    @staticmethod
    def _eq_preset(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        name = _as_enum(params.get("preset"), "EQ preset", set(EQ_PRESETS))
        bands = [
            {"index": i, "filter": band[0], "freq": band[1], "gain": value * 10, "q": 124}
            for i, (band, value) in enumerate(zip(
                [("LOWSHELF", 80), ("PEAKING", 120), ("PEAKING", 315), ("PEAKING", 800),
                 ("PEAKING", 2000), ("PEAKING", 5000), ("HIGHSHELF", 8000)],
                EQ_PRESETS[name],
            ))
        ]
        bands[0]["q"] = 80
        bands[6]["q"] = 80
        return "/zone/audio", {"zone": ZONE, "user_eq_bands": _eq_string(bands)}

    def _media_add(self, params: dict[str, Any]) -> None:
        self.media.add(self.state, params.get("item"), params.get("action", "APPEND"))
        return None

    def _media_play(self, params: dict[str, Any]) -> None:
        self.media.add(self.state, params.get("item"), "PLAY_NOW")
        return None

    def _media_play_next(self, params: dict[str, Any]) -> None:
        self.media.add(self.state, params.get("item"), "PLAY_NEXT")
        return None

    def run(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        handler = self.actions.get(name)
        if handler is None:
            raise SMOIPError(f"Unknown action {name!r}")
        woke = False
        if name not in self.NO_WAKE_ACTIONS:
            woke = self.wake_if_needed()
        if woke and name == "toggle":
            # The play button means "start playing" while the player is asleep.
            # Sending toggle after a wake could pause a source that resumed.
            handler = self.actions["play"]
        command = handler(params)
        if command is None:
            return {"ok": True}
        path, arguments = command
        self.state.client.request(path, arguments)
        self.state.refresh()
        return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    server_version = "CXN100Controller/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> "AppServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            # Browser tabs often disappear mid-request during a shutdown.
            pass

    def _send(self, status: HTTPStatus, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _static(self, name: str) -> None:
        target = (WEB_ROOT / name).resolve()
        if not str(target).startswith(str(WEB_ROOT.resolve())) or not target.is_file():
            self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        types = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml",
                 ".png": "image/png", ".ico": "image/x-icon"}
        self._send(HTTPStatus.OK, target.read_bytes(), types.get(target.suffix, "application/octet-stream"))

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._static("index.html")
        elif path == "/app.js":
            self._static("app.js")
        elif path == "/styles.css":
            self._static("styles.css")
        elif path == "/favicon.ico":
            self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
        elif path == "/logo.png":
            self._static("logo.png")
        elif path == "/api/browse":
            query = parse_qs(urlparse(self.path).query)
            try:
                object_id = query.get("object", ["0"])[0]
                start = _as_int(query.get("start", ["0"])[0], "start", 0, 100000)
                count = _as_int(query.get("count", ["100"])[0], "count", 1, 200)
                # Opening the library is a deliberate action, so wake a sleeping
                # player before asking its UPnP server for USB drives.
                woke = self.app.actions.wake_if_needed()
                result = self.app.actions.media.browse(self.app.state, object_id, start, count)
                if woke and object_id == "0":
                    # The UPnP server can take several seconds to remount USB
                    # storage after wake. Poll the root briefly so the first
                    # browse does not report "no drives" incorrectly.
                    deadline = time.monotonic() + 6.0
                    while not result.get("items") and time.monotonic() < deadline:
                        time.sleep(1.0)
                        result = self.app.actions.media.browse(
                            self.app.state, object_id, start, count
                        )
                self._json(result)
            except (ValueError, SMOIPError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except (upnp.UPnPError, OSError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
        elif path == "/api/config":
            self._json(self.app.state.config())
        elif path == "/api/playlists":
            self._json({"playlists": self.app.playlist_summaries(),
                        "path": str(self.app.playlists.path)})
        elif path.startswith("/api/playlists/"):
            playlist_id = path.rsplit("/", 1)[-1]
            resolved = self.app.resolve_playlist(playlist_id)
            if resolved is None:
                self._json({"error": "Playlist not found"}, HTTPStatus.NOT_FOUND)
            else:
                self._json({"playlist": resolved})
        elif path == "/api/library/status":
            status = self.app.index.status_snapshot()
            status["ai_configured"] = bool(os.environ.get("HIFIMAGIC_AI_KEY", "").strip())
            status["path"] = str(self.app.index.path)
            self._json(status)
        elif path == "/api/library/health":
            self._json(self.app.index.health_snapshot())
        elif path == "/api/radio/filters":
            self._json(self.app.radio.filters())
        elif path == "/api/radio/favourites":
            self._json({"items": self.app.radio.store.favourites()})
        elif path == "/api/library/search":
            query = parse_qs(urlparse(self.path).query)
            text = query.get("q", [""])[0]
            limit = _as_int(query.get("limit", ["100"])[0], "limit", 1, 5000)
            use_ai = str(query.get("ai", ["0"])[0]).lower() in ("1", "true", "yes")
            try:
                self._json(self.app.index.search(text, limit, use_ai))
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/meter":
            if self.client_address[0] not in LOOPBACK_ADDRESSES:
                self._json({"error": "Local audio is only listed on this computer"}, HTTPStatus.FORBIDDEN)
                return
            folder = self.app.state.meter_folder()
            files, error = _meter_files(folder)
            self._json({"folder": folder, "files": files, "error": error})
        elif path == "/api/meter/file":
            if self.client_address[0] not in LOOPBACK_ADDRESSES:
                self._json({"error": "Local audio is only served to this computer"}, HTTPStatus.FORBIDDEN)
                return
            name = parse_qs(urlparse(self.path).query, keep_blank_values=True).get("name", [""])[0]
            if not name or len(name) > 255 or Path(name).name != name or name in (".", ".."):
                self._json({"error": "Invalid audio file name"}, HTTPStatus.BAD_REQUEST)
                return
            if Path(name).suffix.casefold() not in METER_AUDIO_EXTENSIONS:
                self._json({"error": "Unsupported audio file"}, HTTPStatus.BAD_REQUEST)
                return
            root = _meter_root(self.app.state.meter_folder())
            if root is None:
                self._json({"error": "No audio folder configured"}, HTTPStatus.NOT_FOUND)
                return
            try:
                target = (root / name).resolve()
                if target.parent != root or not target.is_file():
                    raise FileNotFoundError(name)
                stat = target.stat()
                if stat.st_size > METER_MAX_FILE_BYTES:
                    raise ValueError("Audio file is too large")
                self._send(
                    HTTPStatus.OK, target.read_bytes(),
                    METER_AUDIO_MIME.get(target.suffix.casefold(), "application/octet-stream"),
                )
            except FileNotFoundError:
                self._json({"error": "Audio file not found"}, HTTPStatus.NOT_FOUND)
            except (ValueError, OSError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/state":
            self._json(self.app.state.state())
        elif path == "/api/events":
            self._events()
        elif path == "/api/health":
            self._json({"ok": True, "connected": self.app.state.client.connected})
        elif path == "/api/discover":
            try:
                devices = discovery.discover_devices(timeout=2.5, max_workers=200)
                self._json({"ok": True, "devices": devices})
            except OSError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
        elif path == "/api/keepalive":
            params = parse_qs(urlparse(self.path).query)
            if self.client_address[0] in ("127.0.0.1", "::1"):
                self.app.note_client(_client_id(params.get("client", [""])[0]))
            self._json({"ok": True})
        else:
            self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/shutdown":
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                self._json(
                    {"error": "Only local requests can stop HifiMagic"},
                    HTTPStatus.FORBIDDEN,
                )
                return
            params = parse_qs(parsed.query)
            self.app.client_gone(_client_id(params.get("client", [""])[0]))
            self._json({"ok": True})
            return
        if path == "/api/backup/export":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 65536:
                    raise backup.BackupError("Invalid request body")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise backup.BackupError("Expected a JSON object")
                data = backup.create(self.app.index.path.parent, payload.get("ui"))
                filename = "hifimagic-backup-" + time.strftime("%Y%m%d") + ".zip"
                self._send(
                    HTTPStatus.OK, data, "application/zip",
                    {"Content-Disposition": f'attachment; filename="{filename}"'},
                )
            except (ValueError, UnicodeDecodeError, backup.BackupError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except OSError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
            return
        if path == "/api/backup/restore":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > backup.MAX_ZIP_BYTES:
                    raise backup.BackupError("Backup file is empty or too large")
                result = backup.restore(self.rfile.read(length), self.app.index.path.parent)
                self.app.reload_data()
                self._json(result)
            except (ValueError, backup.BackupError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except OSError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
            return
        allowed = {
            "/api/action", "/api/connect", "/api/playlists", "/api/playlists/add",
            "/api/playlists/delete", "/api/playlists/smart", "/api/playlist/play",
            "/api/library/reindex", "/api/library/deep-scan", "/api/artist-info",
            "/api/radio/search", "/api/radio/favourites/add", "/api/radio/favourites/remove",
            "/api/meter/config",
        }
        if path not in allowed:
            self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 65536:
                raise SMOIPError("Invalid request body")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise SMOIPError("Expected a JSON object")
            if path == "/api/connect":
                config = self.app.state.configure(payload.get("host"), payload.get("port"))
                self.app.actions.media.reset()
                self._json({"ok": True, "config": config})
            elif path == "/api/meter/config":
                if self.client_address[0] not in LOOPBACK_ADDRESSES:
                    self._json({"error": "Audio folder can only be changed on this computer"}, HTTPStatus.FORBIDDEN)
                    return
                config = self.app.state.set_meter_folder(payload.get("folder"))
                self._json({"ok": True, "config": config})
            elif path == "/api/playlists":
                record = self.app.playlists.upsert(payload.get("playlist") or {})
                self._json({"ok": True, "playlist": record})
            elif path == "/api/playlists/add":
                self._json(self.app.add_to_playlist(payload))
            elif path == "/api/playlist/play":
                woke = self.app.actions.wake_if_needed()
                if woke:
                    self.app.wait_for_playlist(payload.get("items"))
                try:
                    result = self.app.play_playlist(payload.get("items"))
                except SMOIPError as exc:
                    if not woke or "no items the player can play" not in str(exc):
                        raise
                    # Hierarchical warm-up usually succeeds just before this,
                    # but one transient UNKNOWN_OBJECT is worth a bounded retry.
                    LOG.debug("Playback paths still warming; retrying playlist once")
                    time.sleep(5.0)
                    result = self.app.play_playlist(payload.get("items"))
                self._json(result)
            elif path == "/api/playlists/delete":
                playlist_id = str(payload.get("id") or "")
                if not self.app.playlists.delete(playlist_id):
                    self._json({"error": "Playlist not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._json({"ok": True})
            elif path == "/api/playlists/smart":
                self._json(self.app.save_smart_playlist(payload))
            elif path == "/api/library/reindex":
                self.app.actions.wake_if_needed()
                if not self.app.start_scan():
                    self._json({"error": "A library scan is already running"}, HTTPStatus.CONFLICT)
                else:
                    self._json({"ok": True})
            elif path == "/api/library/deep-scan":
                self.app.actions.wake_if_needed()
                if not self.app.start_deep_scan():
                    self._json({"error": "A library scan is already running"}, HTTPStatus.CONFLICT)
                else:
                    self._json({"ok": True})
            elif path == "/api/artist-info":
                self._json(self.app.artist_info(payload))
            elif path == "/api/radio/search":
                self._json(self.app.radio.search(payload))
            elif path == "/api/radio/favourites/add":
                station = RadioDirectory.clean_station(payload.get("station"))
                self._json({"items": self.app.radio.store.add_favourite(station)})
            elif path == "/api/radio/favourites/remove":
                station_id = _as_text(payload.get("uuid"), "station id", 64)
                self._json({"items": self.app.radio.store.remove_favourite(station_id)})
            else:
                result = self.app.actions.run(str(payload.get("action", "")), payload.get("params") or {})
                self._json(result)
        except (ValueError, SMOIPError, storage.StorageError, upnp.UPnPError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except OSError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
        except Exception as exc:  # pragma: no cover - defensive
            LOG.exception("Request failed")
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _events(self) -> None:
        listener = self.app.state.add_listener()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b"event: hello\ndata: {}\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = listener.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps(event, separators=(",", ":")).encode("utf-8")
                self.wfile.write(b"event: " + event.get("event", "state").encode() + b"\n")
                self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.state.remove_listener(listener)


class RadioDirectory:
    """Opt-in station lookup against the public radio-browser.info directory.

    Only fixed API paths on known mirrors are contacted, never a URL supplied
    by the browser. Results are cached locally and saved stations stay local.
    """

    HOSTS = ("de1.api.radio-browser.info", "at1.api.radio-browser.info", "fi1.api.radio-browser.info")
    USER_AGENT = "HifiMagic/1.0.10 (local media manager; https://github.com/iris3th/hifimagic)"
    ORDERS = {"clickcount", "votes", "name", "bitrate"}
    _COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")
    _TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,59}$")
    _UUID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")

    def __init__(self, store: storage.RadioStore) -> None:
        self.store = store

    def _get(self, path: str, params: dict[str, Any], timeout: float = 12.0) -> Any:
        query = urlencode({key: value for key, value in params.items() if value not in (None, "")})
        for host in self.HOSTS:
            request = Request(
                f"https://{host}{path}?{query}",
                headers={"User-Agent": self.USER_AGENT, "Accept": "application/json"},
            )
            try:
                with urlopen(request, timeout=timeout) as response:
                    if response.status != 200:
                        continue
                    return json.load(response)
            except (OSError, URLError, TimeoutError, ValueError) as exc:
                LOG.debug("Station directory lookup via %s failed: %s", host, exc)
        return None

    @classmethod
    def clean_station(cls, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise SMOIPError("No station supplied")
        station_id = str(raw.get("uuid") or raw.get("stationuuid") or "").strip()
        if not cls._UUID_RE.fullmatch(station_id):
            raise SMOIPError("Invalid station id")
        name = _as_text(raw.get("name"), "station name", 120)
        url = _as_text(raw.get("url"), "stream URL", 512)
        if not url.lower().startswith(("http://", "https://")):
            raise SMOIPError("Station URL must start with http:// or https://")
        favicon = str(raw.get("favicon") or "").strip()
        if not favicon.lower().startswith(("http://", "https://")) or len(favicon) > 512:
            favicon = ""
        country = str(raw.get("country") or "").strip()[:80]
        code = str(raw.get("countrycode") or "").strip().upper()
        if not cls._COUNTRY_RE.fullmatch(code):
            code = ""
        language = str(raw.get("language") or "").strip().lower()[:40]
        tags = str(raw.get("tags") or "").strip()[:200]
        codec = str(raw.get("codec") or "").strip().upper()[:16]
        try:
            bitrate = max(0, min(32000, int(raw.get("bitrate") or 0)))
        except (TypeError, ValueError):
            bitrate = 0
        return {
            "uuid": station_id, "name": name, "url": url, "favicon": favicon,
            "tags": tags, "country": country, "countrycode": code,
            "language": language, "codec": codec, "bitrate": bitrate,
        }

    def filters(self) -> dict[str, Any]:
        cached = self.store.filters()
        if cached:
            return cached
        countries_raw = self._get("/json/countries", {"order": "name"}, 12)
        tags_raw = self._get(
            "/json/tags", {"limit": 40, "order": "stationcount", "reverse": "true"}, 12
        )
        countries: list[dict[str, str]] = []
        for entry in countries_raw if isinstance(countries_raw, list) else []:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("iso_3166_1") or entry.get("code") or "").strip().upper()
            name = str(entry.get("name") or "").strip()
            if re.fullmatch(r"[A-Z]{2}", code) and name:
                countries.append({"code": code, "name": name[:80]})
        tags: list[dict[str, Any]] = []
        for entry in tags_raw if isinstance(tags_raw, list) else []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if name and self._TAG_RE.fullmatch(name):
                try:
                    count = max(0, min(1000000, int(entry.get("stationcount") or 0)))
                except (TypeError, ValueError):
                    count = 0
                tags.append({"name": name, "stations": count})
        result = {"countries": countries[:300], "tags": tags[:40]}
        if countries and tags:
            self.store.save_filters(result)
        return result

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query") or "").strip()[:120]
        country = str(payload.get("country") or "").strip().upper()
        if country and not self._COUNTRY_RE.fullmatch(country):
            raise SMOIPError("Invalid country code")
        tag = str(payload.get("tag") or "").strip()
        if tag and not self._TAG_RE.fullmatch(tag):
            raise SMOIPError("Invalid genre")
        language = str(payload.get("language") or "").strip().lower()
        if language and not re.fullmatch(r"[a-z]{1,20}", language):
            raise SMOIPError("Invalid language")
        minimum = _as_int(payload.get("bitrate_min") or 0, "bitrate", 0, 32000)
        order = _as_enum(str(payload.get("order") or "clickcount"), "order", self.ORDERS)
        limit = _as_int(payload.get("limit") or 24, "limit", 1, 60)
        refresh = _as_bool(payload.get("refresh", False))

        key = json.dumps([query, country, tag, language, minimum, order, limit])
        if not refresh:
            cached = self.store.search(key)
            if cached is not None:
                return {"ok": True, "items": cached, "cached": True, "count": len(cached)}

        data = self._get("/json/stations/search", {
            "limit": limit, "hidebroken": "true", "order": order, "reverse": "true",
            "name": query, "countrycode": country, "tag": tag,
            "language": language, "bitrate_min": minimum or "",
        }, 12)
        if not isinstance(data, list):
            return {
                "ok": True, "items": [], "cached": False,
                "error": "Station directory is unavailable right now. Please try again later.",
            }
        items: list[dict[str, Any]] = []
        for raw in data:
            try:
                items.append(self.clean_station(raw))
            except SMOIPError:
                continue
        if not refresh:
            self.store.save_search(key, items)
        return {"ok": True, "items": items, "cached": False, "count": len(items)}


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        state: PlayerState,
        actions: ActionHandler,
        playlists: storage.PlaylistStore,
        index: library.LibraryIndex,
        artists: storage.ArtistInfoStore,
        radio: RadioDirectory,
    ) -> None:
        super().__init__(address, Handler)
        self.state = state
        self.actions = actions
        self.playlists = playlists
        self.index = index
        self.artists = artists
        self.radio = radio
        self._shutdown_lock = threading.Lock()
        self._shutdown_timer: threading.Timer | None = None
        self._clients: dict[str, float] = {}
        self._ever_seen = False
        self._watchdog_stop = threading.Event()
        threading.Thread(
            target=self._watch_clients, name="hifimagic-lifecycle", daemon=True
        ).start()

    # ------------------------------------------------------------- lifecycle

    def _active_clients(self) -> dict[str, float]:
        """Drop stale heartbeats and return the clients still considered open."""
        now = time.monotonic()
        self._clients = {
            client_id: seen for client_id, seen in self._clients.items()
            if now - seen <= _CLIENT_TTL
        }
        return self._clients

    def note_client(self, client_id: str) -> None:
        """A page is open: remember it and cancel any pending exit."""
        with self._shutdown_lock:
            self._clients[client_id] = time.monotonic()
            self._ever_seen = True
            if self._shutdown_timer is not None:
                self._shutdown_timer.cancel()
                self._shutdown_timer = None

    def client_gone(self, client_id: str = "") -> None:
        """One page closed; only exit if no other page is still responding."""
        with self._shutdown_lock:
            if client_id:
                self._clients.pop(client_id, None)
            else:
                self._clients.clear()
            others = self._active_clients()
        if not others:
            self.request_shutdown(_SHUTDOWN_DELAY)

    def request_shutdown(self, delay: float = _SHUTDOWN_DELAY) -> None:
        """Stop the local controller shortly after the last page goes away."""
        with self._shutdown_lock:
            if self._shutdown_timer is not None:
                return

            def finish() -> None:
                with self._shutdown_lock:
                    self._shutdown_timer = None
                print("Shutting down…", flush=True)
                self.shutdown()

            timer = threading.Timer(delay, finish)
            timer.daemon = True
            self._shutdown_timer = timer
            timer.start()

    def cancel_shutdown(self) -> None:
        """Cancel a pending exit when a page is still open."""
        with self._shutdown_lock:
            if self._shutdown_timer is not None:
                self._shutdown_timer.cancel()
                self._shutdown_timer = None

    def _watch_clients(self) -> None:
        """Fallback for crashed browsers whose close beacon never arrives."""
        while not self._watchdog_stop.wait(5.0):
            with self._shutdown_lock:
                if not self._ever_seen or self._shutdown_timer is not None:
                    continue
                if self._active_clients():
                    continue
            LOG.info("No browser heartbeat for %.0fs; exiting", _CLIENT_TTL)
            self.request_shutdown(_SHUTDOWN_DELAY)
            # Keep watching: a late heartbeat can cancel this shutdown, and the
            # next browser close must still have a watchdog fallback.

    # ----------------------------------------------------------- local data

    def reload_data(self) -> None:
        """Reload disk-backed stores after a backup restore."""
        directory = self.index.path.parent
        self.index.stop()
        self.playlists = storage.PlaylistStore(directory)
        self.index = library.LibraryIndex(directory)
        self.artists = storage.ArtistInfoStore(directory)
        self.radio = RadioDirectory(storage.RadioStore(directory))
        self.actions.media.reset()

    def wait_for_usb(self, timeout: float = 12.0) -> None:
        """Give the player's UPnP server time to remount USB storage after wake."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                root = self.actions.media.browse(self.state, "0", 0, 10)
                for entry in root.get("items") or []:
                    if not entry.get("is_container"):
                        continue
                    children = self.actions.media.browse(
                        self.state, str(entry.get("id") or ""), 0, 10
                    )
                    for child in children.get("items") or []:
                        if not child.get("is_container"):
                            LOG.debug(
                                "USB ready after wake: %s -> %s",
                                entry.get("id"), child.get("id"),
                            )
                            return
                        tracks = self.actions.media.browse(
                            self.state, str(child.get("id") or ""), 0, 1
                        )
                        if tracks.get("items"):
                            LOG.debug(
                                "USB ready after wake: %s -> %s -> %s",
                                entry.get("id"), child.get("id"), tracks["items"][0].get("id"),
                            )
                            return
            except (SMOIPError, upnp.UPnPError, OSError):
                pass
            if time.monotonic() >= deadline:
                LOG.debug("USB was still not browsable after wake")
                return
            time.sleep(0.8)

    def wait_for_playlist(self, raw_items: Any, timeout: float = 15.0) -> None:
        """Warm the USB branch that holds a saved playlist after wake.

        The ContentDirectory on this player only answers BrowseDirectChildren
        for a folder once its ancestors have been browsed. ``resolve()`` now
        does that walk, so one successful warm-up lets the real playlist play
        call use live DIDL immediately. Failures are bounded and harmless.
        """
        sample: dict[str, Any] | None = None
        for raw in raw_items if isinstance(raw_items, list) else []:
            if isinstance(raw, dict) and raw.get("kind") == "media":
                sample = raw
                break
        if sample is None:
            return
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self.actions.media.resolve(self.state, sample) is not None:
                    LOG.debug("Playlist USB branch is ready after wake")
                    return
            except (SMOIPError, upnp.UPnPError, OSError):
                pass
            if time.monotonic() >= deadline:
                LOG.debug("Playlist USB branch was not ready after wake")
                return
            time.sleep(0.5)

    # ---------------------------------------------------------- artist notes

    def _fetch_wikipedia_bio(self, artist: str) -> dict[str, Any] | None:
        """Fetch one short summary on explicit request and cache it locally."""
        url = (
            "https://en.wikipedia.org/api/rest_v1/page/summary/"
            + quote(artist.replace(" ", "_"), safe="")
        )
        request = Request(
            url,
            headers={
                "User-Agent": "HifiMagic/1.0.10 (local media manager; https://github.com/iris3th/hifimagic)",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=6) as response:
                if response.status != 200:
                    return None
                data = json.load(response)
        except (OSError, URLError, TimeoutError, ValueError) as exc:
            LOG.warning("Artist bio lookup failed for %r: %s", artist, exc)
            return None
        summary = str(data.get("extract") or "").strip()
        if not summary or len(summary) > 2000:
            return None
        pages = (data.get("content_urls") or {}).get("desktop") or {}
        page = str(pages.get("page") or "")
        return self.artists.save(artist, summary, page)

    def artist_info(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return local artist facts plus an opt-in, cached Wikipedia summary."""
        artist = storage.clean_text(payload.get("artist"), "artist name", 200)
        fetch = payload.get("fetch") is True or str(payload.get("fetch") or "").lower() in (
            "1", "true", "yes",
        )
        rules = {"all": [{"field": "artist", "op": "equals", "value": artist}]}
        items = self.index.evaluate(rules, 5000)["items"]
        if not items:
            items = self.index.evaluate(
                {"all": [{"field": "artist", "op": "contains", "value": artist}]}, 5000
            )["items"]

        albums: list[str] = []
        genres: list[str] = []
        tracks: list[dict[str, str]] = []
        for item in items:
            album = str(item.get("album") or "").strip()
            genre = str(item.get("genre") or "").strip()
            if album and album not in albums:
                albums.append(album)
            if genre and genre not in genres:
                genres.append(genre)
            tracks.append({
                "title": str(item.get("title") or "").strip(),
                "album": album,
                "id": str(item.get("id") or "").strip(),
            })
        tracks.sort(key=lambda entry: (entry["album"].casefold(), entry["title"].casefold()))

        bio = self.artists.get(artist)
        fetched_now = False
        error = ""
        if fetch and bio is None:
            bio = self._fetch_wikipedia_bio(artist)
            fetched_now = bio is not None
            if bio is None:
                error = "Could not fetch a bio right now. Local details are still available."

        return {
            "ok": True,
            "artist": artist,
            "local_tracks": len(items),
            "album_count": len(albums),
            "albums": albums[:12],
            "genres": genres[:8],
            "sample_tracks": tracks[:4],
            "bio": bio,
            "fetched_now": fetched_now,
            "error": error,
        }

    # ------------------------------------------------------------- playlists

    def playlist_summaries(self) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for record in self.playlists.list():
            if record.get("kind") == "smart":
                manual = list(record.get("manual_items") or [])
                matched = self.index.evaluate(record.get("rules") or {}, 5000)["items"]
                count = len(_unique_playlist_items(manual + matched))
            else:
                count = len(record.get("items") or [])
            summaries.append({**record, "count": count})
        return summaries

    def resolve_playlist(self, playlist_id: str) -> dict[str, Any] | None:
        record = self.playlists.get(playlist_id)
        if record is None:
            return None
        if record.get("kind") == "smart":
            result = self.index.evaluate(record.get("rules") or {}, 5000)
            items = _unique_playlist_items(
                list(record.get("manual_items") or []) + result["items"]
            )
        else:
            items = record.get("items") or []
        return {**record, "items": items, "count": len(items)}

    def save_smart_playlist(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = storage.clean_text(payload.get("name"), "playlist name", 120)
        query = str(payload.get("query") or "").strip()
        if not query:
            raise SMOIPError("Enter a search phrase first")
        parsed = library.parse_query(query, bool(payload.get("ai")))
        mode = str(payload.get("mode") or "create")
        if mode not in ("create", "merge", "overwrite"):
            raise SMOIPError("Invalid smart playlist mode")

        playlist_id = str(payload.get("id") or "").strip()
        existing = self.playlists.get(playlist_id) if playlist_id else None
        if existing is None and mode != "create":
            raise SMOIPError("Choose an existing playlist to merge or overwrite")
        if existing is None:
            existing = next(
                (item for item in self.playlists.list()
                 if str(item.get("name", "")).casefold() == name.casefold()),
                None,
            )
        if existing is not None and mode == "create":
            raise SMOIPError(
                f"A playlist named '{name}' already exists. Choose merge or overwrite."
            )

        upsert: dict[str, Any] = {
            "name": name, "kind": "smart", "query": query, "rules": parsed["rules"],
            "items": [], "manual_items": [],
        }
        if existing is not None:
            upsert["id"] = existing["id"]
            if mode == "merge":
                # Keep everything currently visible in the old playlist, then
                # add the new search results. Smart playlists generate their
                # visible items from rules, so resolve them before converting
                # those tracks into manual entries.
                visible = self.resolve_playlist(existing["id"]).get("items") or []
                manual = list(existing.get("manual_items") or []) + list(visible)
                upsert["manual_items"] = _unique_playlist_items(manual)
                matched = self.index.evaluate(parsed["rules"], 5000)["items"]
                upsert["items"] = _unique_playlist_items(
                    upsert["manual_items"] + matched
                )
        record = self.playlists.upsert(upsert)
        return {
            "ok": True,
            "playlist": self.resolve_playlist(record["id"]) or record,
            "source": parsed["source"],
            "mode": mode,
        }

    def add_to_playlist(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Append a library item to a named saved playlist, creating it if new."""
        playlist_id = str(payload.get("id") or "").strip()
        record = self.playlists.get(playlist_id) if playlist_id else None
        if playlist_id and record is None:
            raise SMOIPError("Playlist not found")
        if record is None:
            name = storage.clean_text(payload.get("name"), "playlist name", 120)
            record = next(
                (item for item in self.playlists.list()
                 if str(item.get("name", "")).casefold() == name.casefold()),
                None,
            )
        if record and record.get("kind") == "smart":
            raise SMOIPError("Smart playlists are generated from rules; choose another playlist")

        item = _playlist_media_item(payload.get("item"))
        # Prefer the server's validated browse metadata over browser state.
        try:
            udn = MediaLibrary._server_udn(self.state)
            with self.actions.media.lock:
                cached = self.actions.media.cache.get((udn, item["id"]))
            if cached:
                item = {**item, **{key: cached[key] for key in
                                   ("resource", "didl", "art_url", "path", "parent_id")
                                   if key in cached}}
        except SMOIPError:
            pass

        name = str(record.get("name") if record else payload.get("name"))
        items = [dict(entry) for entry in (record.get("items") or [])] if record else []
        key = f"u:{item['id']}"
        if any(str(entry.get("id")) == item["id"] or entry.get("key") == key for entry in items):
            summary = dict(record) if record else {"name": name, "kind": "static", "items": items}
            return {"ok": True, "playlist": {**summary, "count": len(items)}, "duplicate": True}
        items.append({"key": key, "kind": "media", **item})
        upsert = {"name": name, "kind": "static", "items": items}
        if record:
            upsert["id"] = record["id"]
        saved = self.playlists.upsert(upsert)
        return {"ok": True, "playlist": {**saved, "count": len(items)}, "duplicate": False}

    def _indexed_item_for_metadata(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        """Find a current UPnP item for an old saved entry by id or metadata."""
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else raw
        object_id = str(metadata.get("id") or metadata.get("object_id") or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_:.\-]{1,160}", object_id):
            found = self.index.evaluate(
                {"all": [{"field": "id", "op": "equals", "value": object_id}]}, 1
            )["items"]
            if found:
                return found[0]
        conditions: list[dict[str, str]] = []
        for field in ("title", "artist", "album"):
            value = str(metadata.get(field) or "").strip()
            if value:
                conditions.append({"field": field, "op": "equals", "value": value})
        if not conditions:
            return None
        matches = self.index.evaluate({"all": conditions}, 20)["items"]
        return matches[0] if matches else None

    def play_playlist(self, raw_items: Any) -> dict[str, Any]:
        """Queue library items in order, then start the first playlist entry."""
        if not isinstance(raw_items, list) or not raw_items:
            raise SMOIPError("Playlist is empty")
        if len(raw_items) > 5000:
            raise SMOIPError("Playlist is too large to play at once")

        listing = (self.state.client.get("/queue/list").get("data") or {})
        live_items = list(listing.get("items") or [])
        live_by_id = {
            str(entry.get("id")): entry for entry in live_items if entry.get("id") is not None
        }
        live_by_key = {
            f"q:{entry.get('id')}": entry for entry in live_items if entry.get("id") is not None
        }
        initial_total = int(listing.get("total") or 0)
        targets: list[int] = []
        skipped: list[str] = []
        appended = 0
        resolved_cache: dict[str, dict[str, Any] | None] = {}

        def fresh_item(item: dict[str, Any]) -> dict[str, Any]:
            """Use live UPnP metadata, including a new id prefix after a remount."""
            object_id = str(item.get("id") or "").strip()
            if object_id not in resolved_cache:
                resolved: dict[str, Any] | None = None
                # After wake the ContentDirectory can answer root Browse while
                # deeper folders still return HTTP 500 for a moment.
                for attempt in range(3):
                    try:
                        resolved = self.actions.media.resolve(self.state, item)
                    except (upnp.UPnPError, OSError, SMOIPError):
                        resolved = None
                    if resolved is not None:
                        break
                    if attempt < 2:
                        time.sleep(0.7)
                resolved_cache[object_id] = resolved
            current = resolved_cache[object_id]
            if current is None:
                return item
            return {**item, **{key: current[key] for key in
                               ("id", "parent_id", "didl", "resource", "path", "title")
                               if current.get(key)}}

        def append_item(item: dict[str, Any]) -> bool:
            """Append one track; queue positions are resolved once, after the batch."""
            nonlocal appended
            self.actions.media.add(self.state, fresh_item(item), "APPEND")
            appended += 1
            return True

        for raw in raw_items:
            title = str((raw or {}).get("title") or "item")[:120] if isinstance(raw, dict) else "item"
            if isinstance(raw, dict) and (
                raw.get("kind") == "queue" or str(raw.get("class") or "").startswith("md.")
            ):
                entry = live_by_id.get(str(raw.get("id"))) or live_by_key.get(str(raw.get("key")))
                if entry is not None:
                    targets.append(int(entry.get("position") or 0))
                    continue
                # Queue ids change after a restart or re-scan. Saved queue
                # entries still carry enough metadata to find the USB track.
                indexed = self._indexed_item_for_metadata(raw)
                if indexed is not None:
                    try:
                        if append_item(indexed):
                            continue
                    except (SMOIPError, upnp.UPnPError) as exc:
                        LOG.debug("Could not append saved queue item %s: %s", title, exc)
                skipped.append(title)
                continue
            # Static playlists can outlive a USB remount. Re-resolve them
            # against the current local index, then refresh the DIDL from the
            # live USB tree before sending it to the player.
            if not (isinstance(raw, dict) and raw.get("kind") == "url"):
                indexed = self._indexed_item_for_metadata(raw)
                if indexed is not None:
                    try:
                        if append_item(indexed):
                            continue
                    except (SMOIPError, upnp.UPnPError) as exc:
                        LOG.debug("Could not append saved queue item %s: %s", title, exc)
            try:
                item = _playlist_media_item(raw)
            except SMOIPError:
                skipped.append(title)
                continue
            try:
                append_item(item)
            except (SMOIPError, upnp.UPnPError) as exc:
                LOG.debug("Could not append playlist item %s: %s", title, exc)
                skipped.append(title)

        if not targets and not appended:
            raise SMOIPError("This playlist has no items the player can play right now")

        # The player does not always show appended tracks immediately, so give
        # /queue/list a short chance to catch up before choosing the first one.
        expected_total = initial_total + appended
        final = listing
        deadline = time.monotonic() + 6.0
        while appended:
            final = (self.state.client.get("/queue/list").get("data") or {})
            if int(final.get("total") or 0) >= expected_total:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.3)
        if appended:
            positions = sorted(
                int(entry.get("position")) for entry in (final.get("items") or [])
                if entry.get("position") is not None
                and int(entry.get("position")) >= initial_total
            )
            targets.extend(positions[:appended] or
                           list(range(initial_total, expected_total)))

        if not targets:
            raise SMOIPError("This playlist has no items the player can play right now")
        # Selecting a queue index keeps an already-playing stream running and
        # starts playback from a stopped state on this firmware. Only send a
        # separate play command if it is still stopped after a short check;
        # sending one while it is already playing returns an error or pauses.
        self.state.client.request("/zone/play_control", {"zone": ZONE, "queue_index": targets[0]})
        deadline = time.monotonic() + 2.0
        while True:
            self.state.refresh()
            current = str((self.state.state().get("playback") or {}).get("state") or "").casefold()
            if current == "play" or time.monotonic() >= deadline:
                break
            time.sleep(0.3)
        if current != "play":
            try:
                self.state.client.request(
                    "/zone/play_control", {"zone": ZONE, "match": "none", "action": "play"}
                )
            except SMOIPError:
                pass
        self.state.refresh()
        return {"ok": True, "queued": len(targets), "skipped": skipped}

    def start_scan(self) -> bool:
        udn = MediaLibrary._server_udn(self.state)
        return self.index.start_scan(self.state.device, udn)

    def start_deep_scan(self) -> bool:
        udn = MediaLibrary._server_udn(self.state)
        return self.index.start_health_scan(self.state.device, udn)


def build_server(
    host: str,
    port: int,
    device: str,
    device_port: int = DEFAULT_DEVICE_PORT,
    support_url: str = "",
    config_store: storage.ConfigStore | None = None,
) -> AppServer:
    client = SMOIPClient(device, device_port, timeout=8)
    state = PlayerState(client, device, device_port, support_url, config_store)
    playlists = storage.PlaylistStore()
    index = library.LibraryIndex(storage.data_dir())
    artists = storage.ArtistInfoStore(storage.data_dir())
    radio = RadioDirectory(storage.RadioStore(storage.data_dir()))

    client.on_connection = lambda up: state.on_connection(up)
    client.on_message = lambda path, message: state.on_message(path, message)
    actions = ActionHandler(state)
    # A standby → on transition (front button, remote, or our own wake command)
    # means UPnP paths may have changed. Drop stale browse metadata immediately.
    state.on_wake = actions.media.reset

    def index_watcher() -> None:
        # The UPnP UDN only appears after the first state refresh, so wait for it.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if state.client.connected:
                try:
                    udn = MediaLibrary._server_udn(state)
                    current = app.index
                    if udn and not current.items and not current.is_scanning():
                        current.start_scan(state.device, udn)
                    return
                except SMOIPError:
                    pass
            time.sleep(1)

    app = AppServer((host, port), state, actions, playlists, index, artists, radio)
    state.start()  # connects in the background; the web UI is available immediately
    threading.Thread(target=index_watcher, name="hifimagic-index-watch", daemon=True).start()
    return app

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local, disk-backed storage for HifiMagic playlists.

The app deliberately keeps no user data in the cloud. Playlists live in a
single JSON file under the platform's application-data directory and are
written atomically so a crash cannot corrupt them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse


def data_dir() -> Path:
    """Return the writable data directory, honouring XDG on Linux."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "HifiMagic"
    else:
        configured = os.environ.get("XDG_DATA_HOME", "").strip()
        base = Path(configured) / "hifimagic" if configured else (
            Path.home() / ".local" / "share" / "hifimagic"
        )
    return base


LOG = logging.getLogger("hifimagic.storage")


class StorageError(RuntimeError):
    """Raised for invalid or unwritable playlist data."""


def clean_text(value: Any, name: str, maximum: int = 200) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum:
        raise StorageError(f"{name} must be 1-{maximum} characters")
    return text


class ConfigStore:
    """Persist only the last device address, never credentials or content."""

    _HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or data_dir()
        self.path = self.directory / "config.json"
        self.lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        with self.lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}
            except (OSError, ValueError) as exc:
                LOG.warning("Could not read config: %s", exc)
                return {}
            if not isinstance(raw, dict):
                return {}
            host = str(raw.get("host") or "").strip()
            try:
                port = int(raw.get("port") or 80)
            except (TypeError, ValueError):
                port = 80
            if not self._HOST_RE.fullmatch(host) or not 1 <= port <= 65535:
                return {}
            return {
                "host": host,
                "port": port,
                "name": str(raw.get("name") or "")[:120],
            }

    def save(self, host: Any, port: Any, name: str = "") -> dict[str, Any]:
        clean_host = clean_text(host, "IP address", 253)
        if not self._HOST_RE.fullmatch(clean_host):
            raise StorageError("IP address may contain only letters, numbers, dots, '-' and '_'")
        try:
            clean_port = int(port)
        except (TypeError, ValueError) as exc:
            raise StorageError("Port must be a number") from exc
        if not 1 <= clean_port <= 65535:
            raise StorageError("Port must be between 1 and 65535")
        payload = {
            "host": clean_host,
            "port": clean_port,
            "name": str(name or "")[:120],
        }
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temporary, self.path)
        return payload


class ArtistInfoStore:
    """Small local cache for opt-in artist summaries.

    Bios are fetched only on an explicit user action and stored under the
    application data directory. No account or content browsing is involved.
    """

    VERSION = 1
    _MAX_ENTRIES = 200
    _WIKI_PREFIX = "https://en.wikipedia.org/wiki/"

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or data_dir()
        self.path = self.directory / "artist-info.json"
        self.lock = threading.RLock()
        self._data: dict[str, Any] = {"version": self.VERSION, "artists": {}}
        self.load()

    @staticmethod
    def key(artist: Any) -> str:
        return re.sub(r"\s+", " ", str(artist or "").strip()).casefold()

    def load(self) -> None:
        with self.lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return
            except (OSError, ValueError) as exc:
                LOG.warning("Could not read artist info cache: %s", exc)
                return
            if isinstance(raw, dict) and isinstance(raw.get("artists"), dict):
                self._data = raw

    def _write(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.path)

    def get(self, artist: Any) -> dict[str, Any] | None:
        key = self.key(artist)
        if not key:
            return None
        with self.lock:
            entry = self._data["artists"].get(key)
        if not isinstance(entry, dict):
            return None
        return {
            "name": str(entry.get("name") or artist),
            "summary": str(entry.get("summary") or "")[:2000],
            "url": str(entry.get("url") or ""),
            "fetched": float(entry.get("fetched") or 0),
            "source": "Wikipedia",
        }

    def save(self, artist: Any, summary: Any, url: Any) -> dict[str, Any] | None:
        key = self.key(artist)
        text = str(summary or "").strip()
        link = str(url or "").strip()
        if not key or not text or len(text) > 2000:
            return None
        if not link.startswith(self._WIKI_PREFIX):
            link = self._WIKI_PREFIX + quote(key.replace(" ", "_"), safe="")
        with self.lock:
            self._data["artists"][key] = {
                "name": str(artist).strip()[:200],
                "summary": text,
                "url": link,
                "fetched": time.time(),
            }
            if len(self._data["artists"]) > self._MAX_ENTRIES:
                oldest = sorted(
                    self._data["artists"].items(),
                    key=lambda pair: float(pair[1].get("fetched") or 0),
                )[: len(self._data["artists"]) - self._MAX_ENTRIES]
                for old_key, _ in oldest:
                    self._data["artists"].pop(old_key, None)
            self._write()
        return self.get(artist)


class RadioStore:
    """Local cache and favourites for the opt-in station directory.

    Results are fetched from a public directory only when the user asks for
    them, then cached here with a TTL. Saved stations never leave this device.
    """

    VERSION = 1
    _SEARCH_TTL = 24 * 3600
    _FILTER_TTL = 7 * 24 * 3600
    _MAX_SEARCHES = 24

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or data_dir()
        self.cache_path = self.directory / "radio-cache.json"
        self.favourites_path = self.directory / "radio-favourites.json"
        self.lock = threading.RLock()
        self._cache: dict[str, Any] = {"version": self.VERSION, "filters": {}, "searches": {}}
        self._favourites: list[dict[str, Any]] = []
        self.load()

    @staticmethod
    def _read(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            LOG.warning("Could not read %s: %s", path.name, exc)
            return None

    @staticmethod
    def _write(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def load(self) -> None:
        with self.lock:
            cache = self._read(self.cache_path)
            if isinstance(cache, dict) and isinstance(cache.get("searches"), dict):
                self._cache = cache
            favourites = self._read(self.favourites_path)
            if isinstance(favourites, dict) and isinstance(favourites.get("items"), list):
                self._favourites = [item for item in favourites["items"] if isinstance(item, dict)]

    def filters(self) -> dict[str, Any] | None:
        with self.lock:
            saved = self._cache.get("filters")
        if not isinstance(saved, dict) or not saved.get("updated"):
            return None
        if time.time() - float(saved["updated"] or 0) > self._FILTER_TTL:
            return None
        return saved

    def save_filters(self, filters: dict[str, Any]) -> None:
        with self.lock:
            self._cache["filters"] = {**filters, "updated": time.time()}
            self._write(self.cache_path, self._cache)

    def search(self, key: str) -> list[dict[str, Any]] | None:
        with self.lock:
            entry = self._cache["searches"].get(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("items"), list):
            return None
        if time.time() - float(entry.get("fetched") or 0) > self._SEARCH_TTL:
            return None
        return list(entry["items"])

    def save_search(self, key: str, items: list[dict[str, Any]]) -> None:
        with self.lock:
            searches = self._cache["searches"]
            searches[key] = {"fetched": time.time(), "items": items[:60]}
            while len(searches) > self._MAX_SEARCHES:
                oldest = min(searches, key=lambda name: float(searches[name].get("fetched") or 0))
                searches.pop(oldest, None)
            self._write(self.cache_path, self._cache)

    def clear_searches(self) -> None:
        with self.lock:
            self._cache["searches"] = {}
            self._write(self.cache_path, self._cache)

    def favourites(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(item) for item in self._favourites]

    def add_favourite(self, station: dict[str, Any]) -> list[dict[str, Any]]:
        with self.lock:
            self._favourites = [item for item in self._favourites
                                if item.get("uuid") != station.get("uuid")]
            self._favourites.insert(0, station)
            self._favourites = self._favourites[:100]
            self._write(self.favourites_path, {"items": self._favourites})
        return self.favourites()

    def remove_favourite(self, station_id: str) -> list[dict[str, Any]]:
        with self.lock:
            self._favourites = [item for item in self._favourites
                                if item.get("uuid") != station_id]
            self._write(self.favourites_path, {"items": self._favourites})
        return self.favourites()


def clean_favourite(value: Any) -> dict[str, Any]:
    """Build a small, safe local favourite from a library or queue item."""
    if not isinstance(value, dict):
        raise StorageError("Favourite must be an object")
    def text(name: str, limit: int = 200) -> str:
        return str(value.get(name) or "")[:limit]

    kind = text("kind", 20).strip().lower()
    if kind not in ("track", "album"):
        kind = "track"
    title = text("title")
    artist = text("artist")
    album = text("album")
    key = text("key", 300).strip()
    if not key:
        identity = " | ".join(part for part in (artist, album, title) if part)
        key = f"n:{identity[:280]}" if identity else ""
    if not key:
        raise StorageError("Favourite needs a title or object id")
    return {
        "key": key,
        "kind": kind,
        "title": title,
        "artist": artist,
        "album": album,
        "source": text("source", 80),
        "art_url": text("art_url", 500),
        "id": text("id", 200),
        "parent_id": text("parent_id", 200),
        "class": text("class", 120),
        "is_container": bool(value.get("is_container")),
        "resource": text("resource", 500),
        "parent_title": text("parent_title"),
    }


class FavouriteStore:
    """Local album/track favourites, stored separately from station favourites."""

    VERSION = 1
    _MAX_ITEMS = 500

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or data_dir()
        self.path = self.directory / "favourites.json"
        self.lock = threading.RLock()
        self._items: list[dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            LOG.warning("Could not read %s: %s", self.path.name, exc)
            return
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            self._items = [item for item in data["items"] if isinstance(item, dict)]

    def _write(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"version": self.VERSION, "items": self._items},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def items(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(item) for item in self._items]

    def add(self, value: Any) -> list[dict[str, Any]]:
        item = clean_favourite(value)
        with self.lock:
            self._items = [existing for existing in self._items
                           if existing.get("key") != item["key"]]
            self._items.insert(0, item)
            self._items = self._items[:self._MAX_ITEMS]
            self._write()
        return self.items()

    def remove(self, key: Any) -> list[dict[str, Any]]:
        wanted = str(key or "").strip()[:300]
        if not wanted:
            raise StorageError("Favourite key is required")
        with self.lock:
            self._items = [item for item in self._items if item.get("key") != wanted]
            self._write()
        return self.items()


def clean_music_db_url(value: Any) -> str:
    """Validate a user-supplied JSON endpoint template.

    Only ordinary http(s) URLs are accepted. Credentials and fragments are
    rejected, and the only placeholders are {artist} and {album}; their values
    are percent-encoded by the server before the request is made.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 1000:
        raise StorageError("Custom metadata URL must be at most 1000 characters")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise StorageError("Custom metadata URL must start with http:// or https://")
    if parsed.username or parsed.password:
        raise StorageError("Do not put a username or password in the metadata URL")
    if parsed.fragment:
        raise StorageError("Custom metadata URL must not contain a fragment")
    if any(character.isspace() for character in text):
        raise StorageError("Custom metadata URL must not contain spaces")
    stripped = text.replace("{artist}", "").replace("{album}", "")
    if "{" in stripped or "}" in stripped:
        raise StorageError("Use only {artist} and {album} as URL placeholders")
    if "{artist}" not in text and "{album}" not in text:
        raise StorageError("Include {artist} or {album} in the metadata URL")
    return text


def clean_metadata_link(value: Any) -> str:
    """Keep only an ordinary http(s) link returned by a metadata provider."""
    text = str(value or "").strip()
    if not text or len(text) > 1000:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    if parsed.username or parsed.password:
        return ""
    if any(character.isspace() for character in text):
        return ""
    return text


class AlbumInfoStore:
    """Settings and cache for opt-in album metadata lookups.

    The provider choice and custom URL template live here, not in the browser.
    Fetched album facts are cached locally and included in backup. Nothing is
    contacted until the user asks for album info.
    """

    VERSION = 1
    PROVIDERS = ("local", "musicbrainz", "custom")
    LABELS = {
        "local": "Local library tags",
        "musicbrainz": "MusicBrainz",
        "custom": "Custom JSON endpoint",
    }
    _MAX_ENTRIES = 200

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or data_dir()
        self.path = self.directory / "album-info.json"
        self.lock = threading.RLock()
        self._data: dict[str, Any] = {
            "version": self.VERSION,
            "settings": {"provider": "musicbrainz", "custom_url": ""},
            "albums": {},
        }
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            LOG.warning("Could not read album info cache: %s", exc)
            return
        if not isinstance(raw, dict):
            return
        settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
        provider = str(settings.get("provider") or "musicbrainz")
        custom_url = ""
        try:
            custom_url = clean_music_db_url(settings.get("custom_url"))
        except StorageError:
            pass
        if provider == "custom" and not custom_url:
            provider = "musicbrainz"
        with self.lock:
            self._data = {
                "version": self.VERSION,
                "settings": {
                    "provider": provider if provider in self.PROVIDERS else "musicbrainz",
                    "custom_url": custom_url,
                },
                "albums": raw.get("albums") if isinstance(raw.get("albums"), dict) else {},
            }

    def _write(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.path)

    def settings(self) -> dict[str, Any]:
        with self.lock:
            settings = dict(self._data["settings"])
        return {**settings, "label": self.LABELS.get(settings["provider"], "MusicBrainz")}

    def set_settings(self, provider: Any, custom_url: Any) -> dict[str, Any]:
        wanted = str(provider or "").strip().lower()
        if wanted not in self.PROVIDERS:
            raise StorageError("Unknown album metadata source")
        url = clean_music_db_url(custom_url)
        if wanted == "custom" and not url:
            raise StorageError("Enter a custom URL template first")
        with self.lock:
            self._data["settings"] = {"provider": wanted, "custom_url": url}
            self._write()
        return self.settings()

    @staticmethod
    def key(artist: Any, album: Any) -> str:
        left = re.sub(r"\s+", " ", str(artist or "").strip()).casefold()
        right = re.sub(r"\s+", " ", str(album or "").strip()).casefold()
        return f"{left}\x1f{right}" if left and right else ""

    def get(self, artist: Any, album: Any) -> dict[str, Any] | None:
        key = self.key(artist, album)
        if not key:
            return None
        with self.lock:
            entry = self._data["albums"].get(key)
        if not isinstance(entry, dict):
            return None
        provider = str(entry.get("provider") or "musicbrainz")
        if provider not in self.LABELS:
            provider = "musicbrainz"
        return {
            "title": str(entry.get("title") or "")[:200],
            "artist": str(entry.get("artist") or artist)[:200],
            "album": str(entry.get("album") or album)[:200],
            "year": str(entry.get("year") or "")[:20],
            "genre": str(entry.get("genre") or "")[:300],
            "summary": str(entry.get("summary") or "")[:2000],
            "art_url": clean_metadata_link(entry.get("art_url")),
            "url": clean_metadata_link(entry.get("url")),
            "provider": provider,
            "source": self.LABELS[provider],
            "fetched": float(entry.get("fetched") or 0),
            "cached": True,
        }

    def save(self, artist: Any, album: Any, info: dict[str, Any],
             provider: str) -> dict[str, Any] | None:
        key = self.key(artist, album)
        useful = any(str(info.get(field) or "").strip() for field in
                     ("year", "genre", "summary", "art_url", "url"))
        if not key or not useful:
            return None
        entry = {
            "title": str(info.get("title") or album)[:200],
            "artist": str(info.get("artist") or artist)[:200],
            "album": str(info.get("album") or album)[:200],
            "year": str(info.get("year") or "")[:20],
            "genre": str(info.get("genre") or "")[:300],
            "summary": str(info.get("summary") or "")[:2000],
            "art_url": clean_metadata_link(info.get("art_url")),
            "url": clean_metadata_link(info.get("url")),
            "provider": provider if provider in self.LABELS else "custom",
            "fetched": time.time(),
        }
        with self.lock:
            self._data["albums"][key] = entry
            if len(self._data["albums"]) > self._MAX_ENTRIES:
                oldest = sorted(
                    self._data["albums"].items(),
                    key=lambda pair: float(pair[1].get("fetched") or 0),
                )[: len(self._data["albums"]) - self._MAX_ENTRIES]
                for old_key, _ in oldest:
                    self._data["albums"].pop(old_key, None)
            self._write()
        return self.get(artist, album)


class PlaylistStore:
    """Thread-safe CRUD for named static and smart playlists."""

    VERSION = 1

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or data_dir()
        self.path = self.directory / "playlists.json"
        self.lock = threading.RLock()
        self._data: dict[str, Any] = {"version": self.VERSION, "playlists": []}
        self.load()

    # ------------------------------------------------------------------ disk

    def load(self) -> None:
        with self.lock:
            try:
                raw = self.path.read_text(encoding="utf-8")
                data = json.loads(raw)
                if isinstance(data, dict) and isinstance(data.get("playlists"), list):
                    self._data = data
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as exc:
                # Never let a damaged file stop the app from starting. Keep a
                # timestamped copy so the user can recover it manually.
                LOG.warning("Could not read playlists: %s", exc)
                backup = self.path.with_name(
                    f"playlists.corrupt-{time.strftime('%Y%m%d-%H%M%S')}.json"
                )
                try:
                    self.path.replace(backup)
                    LOG.warning("Moved damaged playlists to %s", backup)
                except OSError as move_error:
                    LOG.warning("Could not move damaged playlists: %s", move_error)
                self._data = {"version": self.VERSION, "playlists": []}

    def _write(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.path)

    # ------------------------------------------------------------------- CRUD

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(item) for item in self._data["playlists"]]

    def get(self, playlist_id: str) -> dict[str, Any] | None:
        with self.lock:
            for item in self._data["playlists"]:
                if item.get("id") == playlist_id:
                    return dict(item)
        return None

    def upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create or update a playlist. The id is generated when absent."""
        name = clean_text(payload.get("name"), "playlist name", 120)
        kind = str(payload.get("kind") or "static")
        if kind not in ("static", "smart"):
            raise StorageError("Playlist kind must be static or smart")
        now = time.time()
        with self.lock:
            playlist_id = str(payload.get("id") or "").strip()
            existing = next(
                (item for item in self._data["playlists"] if item.get("id") == playlist_id),
                None,
            )
            duplicate = next(
                (
                    item for item in self._data["playlists"]
                    if item.get("name", "").casefold() == name.casefold()
                    and item is not existing
                ),
                None,
            )
            if duplicate is not None:
                raise StorageError(f"A playlist named '{name}' already exists")
            if existing is None:
                existing = {
                    "id": "pl_" + uuid.uuid4().hex[:12],
                    "created": now,
                }
                self._data["playlists"].append(existing)
            existing.update(
                {
                    "name": name,
                    "kind": kind,
                    "query": str(payload.get("query") or "")[:300],
                    "rules": payload.get("rules") if isinstance(payload.get("rules"), dict) else {},
                    "items": payload.get("items") if isinstance(payload.get("items"), list) else [],
                    "manual_items": (
                        payload.get("manual_items")
                        if isinstance(payload.get("manual_items"), list) else []
                    ),
                    "updated": now,
                }
            )
            self._write()
            return dict(existing)

    def delete(self, playlist_id: str) -> bool:
        with self.lock:
            before = len(self._data["playlists"])
            self._data["playlists"] = [
                item for item in self._data["playlists"] if item.get("id") != playlist_id
            ]
            if len(self._data["playlists"]) == before:
                return False
            self._write()
            return True

    def is_empty(self) -> bool:
        with self.lock:
            return not self._data["playlists"]

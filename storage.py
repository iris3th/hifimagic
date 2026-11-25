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
from urllib.parse import quote


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


LOG = logging.getLogger("cxn100.storage")


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
            meter_folder = str(raw.get("meter_folder") or "")[:4096]
            host = str(raw.get("host") or "").strip()
            try:
                port = int(raw.get("port") or 80)
            except (TypeError, ValueError):
                port = 80
            if not self._HOST_RE.fullmatch(host) or not 1 <= port <= 65535:
                return {"meter_folder": meter_folder}
            return {
                "host": host,
                "port": port,
                "name": str(raw.get("name") or "")[:120],
                "meter_folder": meter_folder,
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
            "meter_folder": str(self.load().get("meter_folder") or "")[:4096],
        }
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temporary, self.path)
        return payload

    def set_meter_folder(self, folder: Any) -> dict[str, Any]:
        """Persist the optional local folder used to derive peak-meter levels."""
        text = str(folder or "").strip()
        if len(text) > 4096:
            raise StorageError("Audio folder path is too long")
        with self.lock:
            payload = self.load()
            payload["meter_folder"] = text
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

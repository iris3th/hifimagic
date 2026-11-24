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
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any


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

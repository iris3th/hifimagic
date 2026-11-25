# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local backup bundles for HifiMagic settings and library data.

The bundle is a plain zip containing the app's own JSON files plus a signed-by-
nothing manifest with sizes and SHA-256 hashes. It is only read and written on
this computer. Restore never accepts arbitrary paths from the archive: every
entry must be a known file name listed in the manifest.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

FORMAT = "hifimagic-backup"
VERSION = 1
MANIFEST = "manifest.json"
UI_FILE = "ui-settings.json"

DATA_FILES = (
    "config.json",
    "playlists.json",
    "library-index.json",
    "artist-info.json",
    "album-info.json",
    "radio-favourites.json",
    "favourites.json",
    "radio-cache.json",
    UI_FILE,
)

MAX_ZIP_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_UI_BYTES = 2 * 1024 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")


class BackupError(RuntimeError):
    """Raised when a backup cannot be created or safely restored."""


def app_version() -> str:
    try:
        return Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def _clean_ui(value: Any) -> dict[str, Any] | None:
    """Keep only browser-side preferences that the app knows how to restore."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise BackupError("Browser settings must be a JSON object")
    result: dict[str, Any] = {}
    view = value.get("library_view")
    if view in ("icons", "list"):
        result["library_view"] = view
    preset_view = value.get("preset_view")
    if preset_view in ("icons", "list"):
        result["preset_view"] = preset_view
    if isinstance(value.get("advanced_settings"), bool):
        result["advanced_settings"] = value["advanced_settings"]
    working = value.get("working_playlist")
    if working is not None:
        if not isinstance(working, dict) or not isinstance(working.get("items"), list):
            raise BackupError("The working playlist in the backup is invalid")
        if len(_json_bytes(working)) > MAX_UI_BYTES:
            raise BackupError("The working playlist in the backup is too large")
        result["working_playlist"] = working
    return result or None


def create(directory: Path, ui: Any = None) -> bytes:
    """Build a zip bundle from the local JSON data files."""
    directory = Path(directory)
    ui_data = _clean_ui(ui)
    payloads: dict[str, bytes] = {}
    for name in DATA_FILES:
        if name == UI_FILE:
            if ui_data is None:
                continue
            raw = _json_bytes(ui_data)
        else:
            path = directory / name
            if not path.is_file():
                continue
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raise BackupError(f"Cannot read {name}: {exc}") from exc
        if len(raw) > MAX_FILE_BYTES:
            raise BackupError(f"{name} is too large to back up")
        payloads[name] = raw

    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "app_version": app_version(),
        "created": time.time(),
        "files": [
            {
                "name": name,
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            for name, raw in payloads.items()
        ],
    }
    output = io.BytesIO()
    try:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MANIFEST, _json_bytes(manifest))
            for name, raw in payloads.items():
                archive.writestr(name, raw)
    except (OSError, ValueError) as exc:
        raise BackupError(f"Could not create backup: {exc}") from exc
    result = output.getvalue()
    if len(result) > MAX_ZIP_BYTES:
        raise BackupError("The backup is too large")
    return result


def _validate_json(name: str, raw: bytes) -> None:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BackupError(f"{name} is not valid JSON") from exc
    valid = False
    if name == "config.json":
        valid = isinstance(data, dict)
        if valid and data.get("host"):
            host = str(data.get("host"))
            try:
                port = int(data.get("port") or 80)
            except (TypeError, ValueError):
                port = 0
            valid = bool(_HOST_RE.fullmatch(host)) and 1 <= port <= 65535
        if valid and isinstance(data.get("players"), list):
            for player in data["players"]:
                if not isinstance(player, dict):
                    valid = False
                    break
                try:
                    player_port = int(player.get("port") or 80)
                except (TypeError, ValueError):
                    player_port = 0
                if not (_HOST_RE.fullmatch(str(player.get("host") or ""))
                        and 1 <= player_port <= 65535):
                    valid = False
                    break
    elif name == "playlists.json":
        valid = isinstance(data, dict) and isinstance(data.get("playlists"), list)
    elif name == "library-index.json":
        valid = isinstance(data, dict) and isinstance(data.get("items"), list)
    elif name == "artist-info.json":
        valid = isinstance(data, dict) and isinstance(data.get("artists"), dict)
    elif name == "album-info.json":
        valid = isinstance(data, dict) and isinstance(data.get("albums"), dict)
        if valid and "settings" in data:
            valid = isinstance(data["settings"], dict)
        if valid and "scanned" in data:
            valid = isinstance(data["scanned"], dict)
        if valid and "covers" in data:
            valid = isinstance(data["covers"], dict)
    elif name == "radio-favourites.json":
        valid = isinstance(data, dict) and isinstance(data.get("items"), list)
    elif name == "favourites.json":
        valid = isinstance(data, dict) and isinstance(data.get("items"), list)
    elif name == "radio-cache.json":
        valid = isinstance(data, dict) and isinstance(data.get("searches"), dict)
    elif name == UI_FILE:
        valid = isinstance(data, dict)
    if not valid:
        raise BackupError(f"{name} has an unexpected format")


def _read_manifest(archive: zipfile.ZipFile) -> dict[str, Any]:
    info = archive.getinfo(MANIFEST)
    if info.file_size > MAX_MANIFEST_BYTES:
        raise BackupError("The backup manifest is too large")
    try:
        manifest = json.loads(archive.read(MANIFEST).decode("utf-8"))
    except (
        UnicodeDecodeError, ValueError, KeyError, OSError,
        zipfile.BadZipFile, RuntimeError,
    ) as exc:
        raise BackupError("The backup manifest is unreadable") from exc
    if not isinstance(manifest, dict):
        raise BackupError("The backup manifest is invalid")
    if manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
        raise BackupError("This is not a supported HifiMagic backup")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise BackupError("The backup manifest has no file list")
    return manifest


def _entries(archive: zipfile.ZipFile, manifest: dict[str, Any]) -> dict[str, tuple[int, str]]:
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise BackupError("The backup contains duplicate files")
    entries: dict[str, tuple[int, str]] = {}
    for entry in manifest["files"]:
        if not isinstance(entry, dict):
            raise BackupError("The backup manifest has an invalid file entry")
        name = entry.get("name")
        size = entry.get("size")
        digest = entry.get("sha256")
        if name not in DATA_FILES or name in entries:
            raise BackupError("The backup contains an unknown or duplicate file")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_FILE_BYTES:
            raise BackupError(f"Invalid size for {name} in the backup")
        if not isinstance(digest, str) or not _SHA_RE.fullmatch(digest):
            raise BackupError(f"Invalid checksum for {name} in the backup")
        entries[name] = (size, digest)
    if set(names) != {"manifest.json", *entries}:
        raise BackupError("The backup contains files that are not in its manifest")
    return entries


def restore(payload: bytes, directory: Path) -> dict[str, Any]:
    """Validate a bundle and replace only the JSON files it actually contains."""
    if not isinstance(payload, (bytes, bytearray)):
        raise BackupError("No backup data supplied")
    payload = bytes(payload)
    if not payload or len(payload) > MAX_ZIP_BYTES:
        raise BackupError("The backup file is empty or too large")
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise BackupError("This is not a valid HifiMagic backup zip") from exc

    with archive:
        manifest = _read_manifest(archive)
        entries = _entries(archive, manifest)
        payloads: dict[str, bytes] = {}
        total = 0
        for name, (size, digest) in entries.items():
            info = archive.getinfo(name)
            if info.file_size != size or info.file_size > MAX_FILE_BYTES:
                raise BackupError(f"{name} has an unexpected size in the backup")
            try:
                raw = archive.read(name)
            except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
                raise BackupError(f"Could not read {name} from the backup") from exc
            if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
                raise BackupError(f"{name} failed its backup checksum")
            total += len(raw)
            if total > MAX_TOTAL_BYTES:
                raise BackupError("The backup contains too much data")
            _validate_json(name, raw)
            payloads[name] = raw

    ui_data: dict[str, Any] = {}
    if UI_FILE in payloads:
        try:
            ui_data = _clean_ui(json.loads(payloads[UI_FILE].decode("utf-8"))) or {}
        except (UnicodeDecodeError, ValueError) as exc:
            raise BackupError("Browser settings in the backup are unreadable") from exc
        payloads[UI_FILE] = _json_bytes(ui_data)

    target = Path(directory)
    try:
        target.mkdir(parents=True, exist_ok=True)
        staging = target / (".restore-" + uuid.uuid4().hex)
        try:
            staging.mkdir()
            for name, raw in payloads.items():
                (staging / name).write_bytes(raw)
            for name in payloads:
                os.replace(staging / name, target / name)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    except OSError as exc:
        raise BackupError(f"Could not restore the backup: {exc}") from exc

    return {
        "ok": True,
        "files": sorted(payloads),
        "ui": ui_data,
        "created": float(manifest.get("created") or 0),
        "app_version": str(manifest.get("app_version") or "unknown"),
    }

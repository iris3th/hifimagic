# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local searchable index and playlist-query engine for HifiMagic.

The index is built with read-only UPnP Browse calls and cached on disk. Search
and smart playlists work entirely offline; an AI provider is optional and is
only contacted when a key is explicitly configured by the user.
"""

from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

import upnp

INDEX_VERSION = 1
_SEARCHABLE = ("title", "artist", "album", "genre", "path")
_STOPWORDS = {
    "all", "me", "my", "the", "a", "an", "some", "please", "give", "get",
    "find", "show", "build", "make", "create", "with", "from", "of", "for",
    "and", "that", "songs", "song", "tracks", "track", "music", "playlist",
    "list", "only", "everything", "every", "my", "i", "want", "like",
}
_THEMES = {
    "christmas": ("christmas", "xmas", "noel", "yule", "jingle"),
    "xmas": ("christmas", "xmas", "noel", "yule", "jingle"),
    "live": ("live", "concert"),
    "instrumental": ("instrumental",),
    "soundtrack": ("soundtrack", "ost", "score"),
    "jazz": ("jazz",),
    "classical": ("classical", "symphony", "orchestra"),
    "ambient": ("ambient", "chillout", "relaxing"),
}
_FIELD_RE = re.compile(r"\b(title|artist|album|genre|path)\s*:\s*([\w\-]+)", re.I)
_TOKEN_RE = re.compile(r"[\w][\w'-]*", re.UNICODE)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _haystack(item: dict[str, Any]) -> str:
    return " ".join(_text(item.get(field)) for field in _SEARCHABLE).casefold()


def _condition(rule: dict[str, Any], item: dict[str, Any]) -> bool:
    field = str(rule.get("field") or "text")
    op = str(rule.get("op") or "contains")
    wanted = _text(rule.get("value")).casefold()
    if not wanted:
        return True
    haystack = _haystack(item) if field == "text" else _text(item.get(field)).casefold()
    if op == "equals":
        return haystack == wanted
    if op == "starts":
        return haystack.startswith(wanted)
    return wanted in haystack


def matches(rules: dict[str, Any], item: dict[str, Any]) -> bool:
    """Evaluate a parsed query rule set against one indexed item."""
    all_rules = rules.get("all") if isinstance(rules.get("all"), list) else []
    any_rules = rules.get("any") if isinstance(rules.get("any"), list) else []
    not_rules = rules.get("not") if isinstance(rules.get("not"), list) else []
    for rule in all_rules:
        if isinstance(rule, dict) and not _condition(rule, item):
            return False
    if any_rules:
        if not any(isinstance(rule, dict) and _condition(rule, item) for rule in any_rules):
            return False
    for rule in not_rules:
        if isinstance(rule, dict) and _condition(rule, item):
            return False
    return True


def local_rules(query: str) -> dict[str, Any]:
    """Translate plain English into a rules object without any network use."""
    raw = _text(query)
    lowered = raw.casefold()
    phrases = re.findall(r'"([^"]+)"', raw)
    remainder = re.sub(r'"[^"]*"', " ", raw)
    fields: list[dict[str, str]] = [
        {"field": match.group(1).casefold(), "op": "contains", "value": match.group(2)}
        for match in _FIELD_RE.finditer(remainder)
    ]
    remainder = _FIELD_RE.sub(" ", remainder)
    tokens = [token for token in _TOKEN_RE.findall(remainder.casefold())
              if token not in _STOPWORDS and len(token) > 1]
    tokens.extend(phrase.casefold() for phrase in phrases)

    any_rules: list[dict[str, str]] = []
    all_rules: list[dict[str, str]] = list(fields)
    for token in tokens:
        synonyms = _THEMES.get(token)
        if synonyms:
            any_rules.extend(
                {"field": "text", "op": "contains", "value": word} for word in synonyms
            )
        else:
            all_rules.append({"field": "text", "op": "contains", "value": token})
    if " or " in f" {lowered} ":
        any_rules.extend(all_rules)
        all_rules = []
    if not all_rules and not any_rules and raw:
        any_rules = [{"field": "text", "op": "contains", "value": lowered}]
    return {"all": all_rules, "any": any_rules, "not": []}


def _ai_configured() -> bool:
    return bool(os.environ.get("HIFIMAGIC_AI_KEY", "").strip())


def ai_rules(query: str) -> dict[str, Any]:
    """Ask an OpenAI-compatible endpoint for rules. Never called implicitly."""
    url = os.environ.get(
        "HIFIMAGIC_AI_URL", "https://api.openai.com/v1/chat/completions"
    ).strip()
    key = os.environ["HIFIMAGIC_AI_KEY"].strip()
    model = os.environ.get("HIFIMAGIC_AI_MODEL", "gpt-4o-mini").strip()
    system = (
        "Convert the user's playlist request into a JSON object with keys all, any "
        "and not. Each is an array of {field, op, value}; field is one of title, "
        "artist, album, genre, path or text; op is contains, equals or starts. "
        "Return only JSON, no prose."
    )
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": query[:300]},
            ],
            "temperature": 0,
            "max_tokens": 300,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = json.loads(response.read().decode("utf-8"))
    content = body["choices"][0]["message"]["content"]
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise ValueError("AI returned no JSON rules")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("AI returned invalid rules")
    return {
        "all": parsed.get("all") if isinstance(parsed.get("all"), list) else [],
        "any": parsed.get("any") if isinstance(parsed.get("any"), list) else [],
        "not": parsed.get("not") if isinstance(parsed.get("not"), list) else [],
    }


def parse_query(query: str, use_ai: bool = False) -> dict[str, Any]:
    """Return the rules for a natural-language playlist request."""
    rules = local_rules(query)
    source = "local"
    if use_ai and _ai_configured():
        try:
            rules = ai_rules(query)
            source = "ai"
        except Exception:
            source = "local"
    return {"query": _text(query), "rules": rules, "source": source}


class LibraryIndex:
    """Persisted, read-only index of the player's USB media library."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / "library-index.json"
        self.lock = threading.RLock()
        self.items: list[dict[str, Any]] = []
        self.status: dict[str, Any] = {
            "state": "idle", "scanned": 0, "total": 0,
            "updated": 0.0, "error": "",
        }
        self._scan_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._health_thread: threading.Thread | None = None
        self._health_stop = threading.Event()
        self.health: dict[str, Any] = {
            "state": "idle", "checked": 0, "containers": 0,
            "missing_count": 0, "stale_count": 0, "duration_count": 0,
            "added_count": 0, "unreadable_count": 0,
            "missing": [], "stale": [], "no_duration": [], "unreadable": [],
            "finished": 0.0, "error": "",
        }
        self.load()

    def stop(self) -> None:
        """Ask any running scan threads to finish before this index is replaced."""
        self._stop.set()
        self._health_stop.set()

    # ------------------------------------------------------------------ disk

    def load(self) -> None:
        with self.lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("items"), list):
                    self.items = data["items"]
                    self.status.update(
                        {
                            "state": "ready" if self.items else "idle",
                            "scanned": len(self.items),
                            "total": len(self.items),
                            "updated": float(data.get("updated") or 0),
                            "error": "",
                        }
                    )
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as exc:
                self.status["error"] = f"Cannot read library index: {exc}"

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        updated = time.time()
        temporary.write_text(
            json.dumps(
                {"version": INDEX_VERSION, "updated": updated, "items": self.items},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
        self.status["updated"] = updated

    # ------------------------------------------------------------------ scan

    def status_snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.status)

    def is_scanning(self) -> bool:
        with self.lock:
            return self.status["state"] == "scanning"

    # ------------------------------------------------------------ deep scan

    def health_snapshot(self) -> dict[str, Any]:
        with self.lock:
            result = dict(self.health)
        for key in ("missing", "stale", "no_duration", "unreadable"):
            result[key] = list(result.get(key) or [])
        return result

    def is_health_scanning(self) -> bool:
        with self.lock:
            return self.health["state"] == "checking"

    def start_health_scan(self, host: str, udn: str) -> bool:
        with self.lock:
            if self.health["state"] == "checking" or self.status["state"] == "scanning":
                return False
            self.health.update({
                "state": "checking", "checked": 0, "containers": 0,
                "missing_count": 0, "stale_count": 0, "duration_count": 0,
                "added_count": 0, "unreadable_count": 0,
                "missing": [], "stale": [], "no_duration": [], "unreadable": [],
                "finished": 0.0, "error": "",
            })
        self._health_stop.clear()
        self._health_thread = threading.Thread(
            target=self._health_worker,
            args=(host, udn),
            name="hifimagic-deep-scan",
            daemon=True,
        )
        self._health_thread.start()
        return True

    def _health_worker(self, host: str, udn: str) -> None:
        with self.lock:
            old_by_id = {str(item.get("id") or ""): dict(item) for item in self.items if item.get("id")}
        collected: list[dict[str, Any]] = []
        seen: set[str] = {"0"}
        unreadable: list[dict[str, str]] = []
        scan_lock = threading.Lock()

        def record(item: dict[str, Any], parent_path: str) -> dict[str, Any]:
            return {
                "id": _text(item.get("id")),
                "parent_id": _text(item.get("parent_id")),
                "title": _text(item.get("title")),
                "artist": _text(item.get("artist")),
                "album": _text(item.get("album")),
                "duration": _text(item.get("duration")),
                "resource": _text(item.get("resource")),
                "path": parent_path,
            }

        def process(object_id: str, parent_path: str, depth: int) -> list[tuple[str, str, int]]:
            if self._health_stop.is_set():
                return []
            children: list[dict[str, Any]] = []
            try:
                children = self._browse_all(host, udn, object_id, stop=self._health_stop)
            except upnp.UPnPError as exc:
                with scan_lock:
                    unreadable.append({"path": parent_path, "error": str(exc)[:160]})
                return []
            found: list[dict[str, Any]] = []
            containers: list[tuple[str, str, int]] = []
            for item in children:
                title = _text(item.get("title"))
                if not item.get("is_container"):
                    found.append(record(item, parent_path))
                elif depth < 12:
                    try:
                        empty = upnp.is_empty_artwork_folder(host, udn, item, timeout=4.0)
                    except upnp.UPnPError:
                        empty = False
                    if empty:
                        continue
                    containers.append(
                        (item.get("id", ""), f"{parent_path} / {title}", depth + 1)
                    )
            with scan_lock:
                collected.extend(found)
            with self.lock:
                self.health["checked"] = len(collected)
                self.health["containers"] = len(seen)
            return containers

        try:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="hifimagic-health") as pool:
                futures = {pool.submit(process, "0", "USB", 0)}
                while futures and not self._health_stop.is_set():
                    done, pending = wait(futures, return_when=FIRST_COMPLETED)
                    futures = set(pending)
                    for future in done:
                        for next_id, next_path, next_depth in future.result():
                            if not next_id or self._health_stop.is_set():
                                continue
                            with scan_lock:
                                if next_id in seen:
                                    continue
                                seen.add(next_id)
                            futures.add(pool.submit(process, next_id, next_path, next_depth))

            if self._health_stop.is_set():
                return
            fresh_by_id = {str(item.get("id") or ""): item for item in collected if item.get("id")}
            missing: list[dict[str, str]] = []
            stale: list[dict[str, str]] = []
            no_duration: list[dict[str, str]] = []
            for object_id, old in old_by_id.items():
                fresh = fresh_by_id.get(object_id)
                if fresh is None:
                    missing.append({
                        "id": object_id,
                        "title": _text(old.get("title")),
                        "path": _text(old.get("path")),
                    })
                    continue
                if _text(old.get("resource")) != _text(fresh.get("resource")):
                    stale.append({
                        "id": object_id,
                        "title": _text(fresh.get("title")) or _text(old.get("title")),
                        "old_path": _text(old.get("resource")),
                        "new_path": _text(fresh.get("resource")),
                    })
                if not _text(fresh.get("duration")):
                    no_duration.append({
                        "id": object_id,
                        "title": _text(fresh.get("title")),
                        "path": _text(fresh.get("path")),
                    })
            added_count = len(set(fresh_by_id) - set(old_by_id))
            with self.lock:
                self.health.update({
                    "state": "ready",
                    "checked": len(fresh_by_id),
                    "containers": len(seen),
                    "missing_count": len(missing),
                    "stale_count": len(stale),
                    "duration_count": len(no_duration),
                    "added_count": added_count,
                    "unreadable_count": len(unreadable),
                    "missing": missing[:40],
                    "stale": stale[:40],
                    "no_duration": no_duration[:40],
                    "unreadable": unreadable[:20],
                    "finished": time.time(),
                    "error": "",
                })
        except Exception as exc:
            with self.lock:
                self.health.update({"state": "error", "error": str(exc), "finished": time.time()})

    def start_scan(
        self,
        host: str,
        udn: str,
        on_complete: Callable[[], None] | None = None,
    ) -> bool:
        if self.is_scanning():
            return False
        with self.lock:
            self.status.update({"state": "scanning", "error": "", "scanned": 0})
        self._stop.clear()
        self._scan_thread = threading.Thread(
            target=self._scan_worker,
            args=(host, udn, on_complete),
            name="hifimagic-library-scan",
            daemon=True,
        )
        self._scan_thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _browse_all(
        self,
        host: str,
        udn: str,
        object_id: str,
        stop: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        stop = stop or self._stop
        items: list[dict[str, Any]] = []
        start = 0
        while not stop.is_set():
            result = upnp.browse(
                host, udn, object_id=object_id, start=start, count=200, timeout=8
            )
            batch = result.get("items", [])
            items.extend(batch)
            returned = int(result.get("number_returned") or len(batch))
            total = int(result.get("total") or len(items))
            start += returned
            if not batch or not returned or returned < 200 or start >= total:
                break
        return items

    def _scan_worker(
        self, host: str, udn: str, on_complete: Callable[[], None] | None
    ) -> None:
        collected: list[dict[str, Any]] = []
        seen: set[str] = {"0"}
        scan_lock = threading.Lock()

        def record(item: dict[str, Any], parent_path: str) -> dict[str, Any]:
            return {
                "id": item.get("id", ""),
                "parent_id": item.get("parent_id", ""),
                "class": _text(item.get("class")) or "object.item.audioItem.musicTrack",
                "is_container": False,
                "title": _text(item.get("title")),
                "artist": _text(item.get("artist")),
                "album": _text(item.get("album")),
                "genre": _text(item.get("genre")),
                "track": _text(item.get("track")),
                "duration": _text(item.get("duration")),
                "resource": _text(item.get("resource")),
                "art_url": _text(item.get("art_url")),
                "path": parent_path,
                "didl": _text(item.get("didl")),
            }

        def process(
            object_id: str, parent_path: str, depth: int
        ) -> list[tuple[str, str, int]]:
            if self._stop.is_set():
                return []
            try:
                children = self._browse_all(host, udn, object_id)
            except upnp.UPnPError:
                # Stale object ids are normal while the player re-indexes.
                if object_id == "0":
                    raise
                return []
            found: list[dict[str, Any]] = []
            containers: list[tuple[str, str, int]] = []
            for item in children:
                title = _text(item.get("title"))
                if not item.get("is_container"):
                    found.append(record(item, parent_path))
                elif upnp.is_empty_artwork_folder(host, udn, item, timeout=4.0):
                    continue
                elif depth < 12:
                    containers.append(
                        (item.get("id", ""), f"{parent_path} / {title}", depth + 1)
                    )
            with scan_lock:
                collected.extend(found)
            with self.lock:
                self.status["scanned"] = len(seen)
                self.status["total"] = len(collected)
            return containers

        try:
            # Four read-only Browse workers keep the initial scan reasonably
            # quick without putting heavy concurrent load on the player.
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="hifimagic-scan") as pool:
                futures = {pool.submit(process, "0", "USB", 0)}
                while futures and not self._stop.is_set():
                    done, pending = wait(futures, return_when=FIRST_COMPLETED)
                    futures = set(pending)
                    for future in done:
                        for next_id, next_path, next_depth in future.result():
                            if not next_id or self._stop.is_set():
                                continue
                            with scan_lock:
                                if next_id in seen:
                                    continue
                                seen.add(next_id)
                            futures.add(pool.submit(process, next_id, next_path, next_depth))

            if self._stop.is_set():
                return
            with self.lock:
                if self.status.get("state") == "error":
                    return
                self.items = collected
                self._write()
                self.status.update(
                    {"state": "ready", "scanned": len(seen), "total": len(collected)}
                )
        except Exception as exc:
            with self.lock:
                self.status.update({"state": "error", "error": str(exc)})
        finally:
            if on_complete and not self._stop.is_set():
                on_complete()

    # ---------------------------------------------------------------- search

    def evaluate(self, rules: dict[str, Any], limit: int = 500) -> dict[str, Any]:
        with self.lock:
            items = [dict(item) for item in self.items]
        results = [item for item in items if matches(rules, item)]
        return {"items": results[:limit], "total": len(results)}

    def album_pairs(self, limit: int = 1000) -> list[dict[str, str]]:
        """Unique artist/album pairs from the indexed USB library."""
        with self.lock:
            items = [dict(item) for item in self.items]
        seen: dict[str, dict[str, str]] = {}
        for item in items:
            artist = re.sub(r"\s+", " ", str(item.get("artist") or "").strip())
            album = re.sub(r"\s+", " ", str(item.get("album") or "").strip())
            if not artist or not album:
                continue
            key = f"{artist.casefold()}\x1f{album.casefold()}"
            if key not in seen:
                seen[key] = {"artist": artist[:200], "album": album[:200]}
        pairs = sorted(
            seen.values(), key=lambda pair: (pair["artist"].casefold(), pair["album"].casefold())
        )
        return pairs[:limit]

    def search(self, query: str, limit: int = 100, use_ai: bool = False) -> dict[str, Any]:
        if not _text(query):
            raise ValueError("Enter a search phrase")
        parsed = parse_query(query, use_ai)
        results = self.evaluate(parsed["rules"], limit)
        return {
            "query": parsed["query"],
            "rules": parsed["rules"],
            "source": parsed["source"],
            "items": results["items"],
            "total": results["total"],
            "indexed": len(self.items),
        }

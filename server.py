# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local HTTP/SSE bridge between the browser UI and the CXN100.

Every command the browser can send goes through an explicit, validated
allow-list here. Arbitrary SMOIP paths from a browser are never forwarded, so
the player cannot be misconfigured or factory-reset from the web UI.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import upnp
from smoip import SMOIPClient, SMOIPError

LOG = logging.getLogger("cxn100.server")
ZONE = "ZONE1"
WEB_ROOT = Path(__file__).resolve().parent / "web"
DEFAULT_DEVICE_PORT = 80
DEFAULT_UPNP_PORT = 8050
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")


def _clean_support_url(value: Any) -> str:
    """Only allow ordinary http(s) links in the footer."""
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return text

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
        with self.lock:
            for item in result["items"]:
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
            if len(self.cache) > 2000:
                self.cache.clear()
        total = int(result["total"])
        return {
            "object_id": object_id,
            "items": items,
            "total": total,
            "start": start,
            "count": len(items),
            "has_more": start + len(items) < total,
            "next_start": start + len(items),
            "server_udn": udn,
        }

    def add(self, state: "PlayerState", item: Any, action: str = "APPEND") -> dict[str, Any]:
        if not isinstance(item, dict):
            raise SMOIPError("No library item supplied")
        action = _as_enum(action, "action", {"APPEND", "PLAY_NOW"})
        object_id = str(item.get("id") or "").strip()
        udn = self._server_udn(state)
        with self.lock:
            cached = self.cache.get((udn, object_id))
        if cached:
            didl = str(cached.get("didl") or upnp.media_didl(cached))
        else:
            clean = upnp.normalize_item(item)
            didl = upnp.media_didl(clean)
        return upnp.queue_add(state.device, state.device_port, udn, didl, action)


class PlayerState:
    """Keeps a snapshot of the player and pushes updates to SSE clients."""

    def __init__(
        self,
        client: SMOIPClient,
        device: str,
        device_port: int = DEFAULT_DEVICE_PORT,
        support_url: str = "",
    ) -> None:
        self.client = client
        self.device = device
        self.device_port = device_port
        self.support_url = _clean_support_url(support_url)
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

    def config(self) -> dict[str, Any]:
        with self.lock:
            return {
                "host": self.device,
                "port": self.device_port,
                "connected": bool(self.snapshot["connected"]),
                "connecting": bool(self.snapshot["connecting"]),
                "last_error": self.snapshot["last_error"],
                "support_url": self.support_url,
            }

    def configure(self, host: Any, port: Any) -> dict[str, Any]:
        """Switch the player connection. The web server never goes down."""
        host = _as_text(host, "IP address", 253)
        if not _HOST_RE.fullmatch(host):
            raise SMOIPError("IP address may contain only letters, numbers, dots, '-' and '_'")
        port = _as_int(port, "port", 1, 65535)

        with self.lock:
            if host == self.device and port == self.device_port and self.client.connected:
                self.snapshot["connecting"] = False
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
            self.snapshot[key] = data
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

    def state(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.snapshot))


class ActionHandler:
    """The only bridge from HTTP requests to device commands."""

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
        }

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

    def run(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        handler = self.actions.get(name)
        if handler is None:
            raise SMOIPError(f"Unknown action {name!r}")
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

    def _send(self, status: HTTPStatus, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

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
                self._json(self.app.actions.media.browse(self.app.state, object_id, start, count))
            except (ValueError, SMOIPError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except (upnp.UPnPError, OSError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
        elif path == "/api/config":
            self._json(self.app.state.config())
        elif path == "/api/state":
            self._json(self.app.state.state())
        elif path == "/api/events":
            self._events()
        elif path == "/api/health":
            self._json({"ok": True, "connected": self.app.state.client.connected})
        else:
            self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path not in ("/api/action", "/api/connect"):
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
                self._json({"ok": True, "config": config})
            else:
                result = self.app.actions.run(str(payload.get("action", "")), payload.get("params") or {})
                self._json(result)
        except (ValueError, SMOIPError, upnp.UPnPError) as exc:
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


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: PlayerState, actions: ActionHandler) -> None:
        super().__init__(address, Handler)
        self.state = state
        self.actions = actions


def build_server(
    host: str,
    port: int,
    device: str,
    device_port: int = DEFAULT_DEVICE_PORT,
    support_url: str = "",
) -> AppServer:
    client = SMOIPClient(device, device_port, timeout=8)
    state = PlayerState(client, device, device_port, support_url)
    client.on_connection = lambda up: state.on_connection(up)
    client.on_message = lambda path, message: state.on_message(path, message)
    actions = ActionHandler(state)
    state.start()  # connects in the background; the web UI is available immediately
    return AppServer((host, port), state, actions)

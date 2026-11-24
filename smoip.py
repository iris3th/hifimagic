# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dependency-free client for Cambridge Audio's SMOIP network control protocol.

The protocol is a plain WebSocket on port 80 at /smoip carrying JSON messages:

    {"path": "/zone/play_state", "params": {"zone": "ZONE1"}}
    {"path": "/zone/play_state", "type": "response", "result": 200,
     "message": "OK", "params": {"zone": "ZONE1", "data": {...}}}

Only standard-library modules are used so the app runs on any Linux box with
Python 3.9+ without installing anything.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
import time
from typing import Any, Callable


class SMOIPError(RuntimeError):
    """Raised when the device cannot be reached or rejects a command."""


class _Waiter:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.message: dict[str, Any] | None = None


class SMOIPClient:
    """Small thread-safe SMOIP client with automatic reconnection."""

    def __init__(
        self,
        host: str,
        port: int = 80,
        *,
        timeout: float = 8.0,
        on_message: Callable[[str, dict[str, Any]], None] | None = None,
        on_connection: Callable[[bool], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.on_message = on_message
        self.on_connection = on_connection

        self._sock: socket.socket | None = None
        self._buf = b""
        self._running = False
        self._reader: threading.Thread | None = None
        self._request_lock = threading.Lock()
        self._cv = threading.Condition()
        self._waiters: dict[str, list[_Waiter]] = {}
        self._connected = False
        self._subscriptions: set[str] = set()
        self._resubscribe_needed = True

    # ------------------------------------------------------------------ status
    @property
    def connected(self) -> bool:
        return self._connected and self._sock is not None

    # ------------------------------------------------------------- connection
    def connect(self) -> None:
        """Open the WebSocket. Safe to call repeatedly."""
        if self.connected:
            return
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(None)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET /smoip HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Origin: http://{self.host}\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                raise SMOIPError("Handshake closed by device")
            head += chunk
        headers, self._buf = head.split(b"\r\n\r\n", 1)
        status = headers.split(b"\r\n", 1)[0].decode("ascii", "replace")
        if "101" not in status:
            sock.close()
            raise SMOIPError(f"WebSocket upgrade failed: {status}")

        self._sock = sock
        self._connected = True
        self._running = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="smoip-reader")
        self._reader.start()
        self._notify_connection(True)

    def close(self) -> None:
        """Close the connection and stop the reader thread."""
        self._running = False
        self._connected = False
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        with self._cv:
            for waiters in self._waiters.values():
                for waiter in waiters:
                    waiter.event.set()
            self._waiters.clear()
        self._notify_connection(False)

    def _notify_connection(self, up: bool) -> None:
        if self.on_connection:
            try:
                self.on_connection(up)
            except Exception:
                pass

    def ensure_connected(self) -> None:
        if self.connected:
            return
        self.close()
        self.connect()

    # ------------------------------------------------------------ frame codec
    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        sock = self._sock
        if sock is None:
            raise SMOIPError("Not connected to device")
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes([0x80 | opcode, 0x80 | length])
        elif length < 65536:
            header = bytes([0x80 | opcode, 0xFE]) + struct.pack("!H", length)
        else:
            header = bytes([0x80 | opcode, 0xFF]) + struct.pack("!Q", length)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        sock.sendall(header + mask + masked)

    def _take(self, count: int) -> bytes:
        while len(self._buf) < count:
            sock = self._sock
            if sock is None:
                raise SMOIPError("Not connected to device")
            chunk = sock.recv(65536)
            if not chunk:
                raise EOFError("Device closed the connection")
            self._buf += chunk
        data, self._buf = self._buf[:count], self._buf[count:]
        return data

    def _recv_frame(self) -> bytes:
        first, second = self._take(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._take(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._take(8))[0]
        mask = self._take(4) if second & 0x80 else None
        payload = self._take(length)
        if mask:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        if opcode == 0x9:  # ping -> pong
            self._send_frame(payload, opcode=0xA)
            return self._recv_frame()
        if opcode == 0x8:
            raise EOFError("Device sent a close frame")
        if opcode not in (0x1, 0x2):
            return self._recv_frame()
        return payload

    # ----------------------------------------------------------------- reader
    def _read_loop(self) -> None:
        while self._running:
            try:
                raw = self._recv_frame()
                message = json.loads(raw.decode("utf-8"))
            except (OSError, EOFError, ValueError) as exc:
                if self._running:
                    self._handle_disconnect(exc)
                return
            path = message.get("path", "")
            if message.get("type") == "response":
                with self._cv:
                    waiters = self._waiters.get(path, [])
                    if waiters:
                        waiter = waiters.pop(0)
                        waiter.message = message
                        waiter.event.set()
            if self.on_message:
                try:
                    self.on_message(path, message)
                except Exception:
                    pass

    def _handle_disconnect(self, exc: BaseException) -> None:
        self.close()
        self._resubscribe_needed = True
        with self._cv:
            for waiters in self._waiters.values():
                for waiter in waiters:
                    waiter.event.set()
            self._waiters.clear()

    # --------------------------------------------------------------- requests
    def request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        retry: bool = True,
    ) -> dict[str, Any]:
        """Send a command and wait for its matching response.

        Returns the whole response message. Raises SMOIPError on failure.
        """
        deadline = time.monotonic() + (timeout or self.timeout)
        for attempt in range(2 if retry else 1):
            try:
                self.ensure_connected()
                waiter = _Waiter()
                with self._cv:
                    self._waiters.setdefault(path, []).append(waiter)
                try:
                    self._send_frame(
                        json.dumps({"path": path, "params": params or {}}).encode("utf-8")
                    )
                except OSError as exc:
                    with self._cv:
                        self._waiters.get(path, []) and self._waiters[path].remove(waiter)
                    self._handle_disconnect(exc)
                    raise SMOIPError(str(exc)) from exc

                remaining = deadline - time.monotonic()
                # Never wait while holding _cv: the reader needs that lock to
                # deliver the response to this waiter.
                ok = waiter.event.wait(max(0.05, remaining))
                if not ok:
                    with self._cv:
                        if waiter in self._waiters.get(path, []):
                            self._waiters[path].remove(waiter)
                if not ok or waiter.message is None:
                    raise SMOIPError(f"Timed out waiting for {path}")
                message = waiter.message
                result = message.get("result")
                if result not in (200, 202, 204):
                    raise SMOIPError(
                        str(message.get("message") or f"Device returned {result}")
                    )
                return message
            except SMOIPError:
                if attempt == 0:
                    self.close()
                    continue
                raise
        raise SMOIPError(f"Request failed: {path}")

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return the ``params`` portion of a successful response."""
        return self.request(path, params).get("params", {})

    def subscribe(self, paths: list[str], zone: str = "ZONE1") -> None:
        """Ask the device to push changes for the given paths."""
        for path in paths:
            self._subscriptions.add(path)
        self._resubscribe_needed = False
        for path in paths:
            try:
                self.request(path, {"update": 100, "zone": zone})
            except SMOIPError:
                pass

    def resubscribe_if_needed(self) -> None:
        if self._resubscribe_needed and self._subscriptions and self.connected:
            paths = list(self._subscriptions)
            self._resubscribe_needed = False
            for path in paths:
                try:
                    self.request(path, {"update": 100, "zone": "ZONE1"})
                except SMOIPError:
                    self._resubscribe_needed = True
                    break

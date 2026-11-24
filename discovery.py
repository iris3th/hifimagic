# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only discovery of Cambridge Audio StreamMagic players on the LAN.

The scan only opens a TCP connection and sends a harmless HTTP HEAD request.
It never writes to a player, and it never guesses credentials or cloud accounts.
"""

from __future__ import annotations

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

_MARKERS = ("streammagic", "cambridge audio")
_PROBE_TIMEOUT = 0.5
_COMMON_SUBNETS = (
    "192.168.49.0/24", "192.168.1.0/24", "192.168.0.0/24",
    "192.168.2.0/24", "192.168.4.0/24", "192.168.10.0/24",
    "192.168.50.0/24", "192.168.100.0/24", "10.0.0.0/24",
    "10.0.1.0/24",
)


def local_ipv4s() -> list[str]:
    """Best-effort list of this computer's private IPv4 addresses."""
    found: set[str] = set()
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect chooses the outgoing interface without sending a packet.
        probe.connect(("8.8.8.8", 80))
        address = str(probe.getsockname()[0])
        parsed = ipaddress.ip_address(address)
        if parsed.is_private and not parsed.is_loopback:
            found.add(address)
    except OSError:
        pass
    finally:
        probe.close()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = str(info[4][0])
            parsed = ipaddress.ip_address(address)
            if parsed.is_private and not parsed.is_loopback:
                found.add(address)
    except (OSError, ValueError):
        pass
    return sorted(found)


def local_subnets() -> list[str]:
    """Local subnet first, then the common home-router ranges."""
    subnets: list[str] = []
    for address in local_ipv4s():
        network = ipaddress.ip_network(f"{address}/24", strict=False)
        text = str(network)
        if text not in subnets:
            subnets.append(text)
    for text in _COMMON_SUBNETS:
        if text not in subnets:
            subnets.append(text)
    return subnets


def probe_host(host: str, port: int = 80, timeout: float = _PROBE_TIMEOUT) -> dict[str, Any] | None:
    """Return a device description when a StreamMagic web server answers."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            request = (
                f"HEAD / HTTP/1.0\r\nHost: {host}\r\n"
                "User-Agent: HifiMagic-discovery/1.0\r\n\r\n"
            )
            sock.sendall(request.encode("ascii"))
            buffer = b""
            while len(buffer) < 1024:
                chunk = sock.recv(512)
                if not chunk:
                    break
                buffer += chunk
                if b"\r\n\r\n" in buffer:
                    break
            headers = buffer.decode("latin-1", "replace")
    except (OSError, ValueError):
        return None
    lowered = headers.casefold()
    if not any(marker in lowered for marker in _MARKERS):
        return None
    server = ""
    for line in headers.splitlines():
        if line.lower().startswith("server:"):
            server = line.split(":", 1)[1].strip()
            break
    return {
        "host": host,
        "port": int(port),
        "name": "Cambridge Audio StreamMagic",
        "model": server or "StreamMagic player",
        "source": "local network scan",
    }


def discover_devices(timeout: float = 3.0, max_workers: int = 64) -> list[dict[str, Any]]:
    """Find StreamMagic players on the local and common home /24 subnets."""
    local = set(local_ipv4s())
    targets: list[str] = []
    for subnet in local_subnets():
        network = ipaddress.ip_network(subnet, strict=False)
        for address in network.hosts():
            host = str(address)
            if host in local:
                continue
            if host not in targets:
                targets.append(host)
    if not targets:
        return []

    devices: list[dict[str, Any]] = []
    pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="hifimagic-find")
    try:
        futures = {pool.submit(probe_host, host): host for host in targets}
        for future in as_completed(futures, timeout=timeout):
            try:
                device = future.result()
            except Exception:
                device = None
            if device:
                devices.append(device)
                if len(devices) >= 5:
                    break
    except Exception:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return sorted(devices, key=lambda item: item["host"])

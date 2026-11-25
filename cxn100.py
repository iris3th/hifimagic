#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Launch the HifiMagic controller for Cambridge Audio network players.

No third-party Python packages are required. Run this file with Python 3.9+
and open the printed URL in a browser, or let the script open it for you.
"""

from __future__ import annotations

import argparse
import errno
import logging
import os
import threading
import webbrowser

import discovery
import storage
from server import build_server, clean_meter_folder

DEFAULT_DEVICE = "192.168.1.20"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="HifiMagic controller for Cambridge Audio network players"
    )
    parser.add_argument("--host", default=None, help="Player IP (default: saved config, then discovery)")
    parser.add_argument("--device-port", type=int, default=None, help="Player port (default: saved config, then 80)")
    parser.add_argument("--port", type=int, default=8765, help="Local web server port (default: %(default)s; next free port is used if busy)")
    parser.add_argument("--bind", default="127.0.0.1", help="Local bind address (default: %(default)s)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser window")
    parser.add_argument(
        "--support-url",
        default=os.environ.get("HIFIMAGIC_SUPPORT_URL", ""),
        help="Optional http(s) support link shown quietly in the footer",
    )
    parser.add_argument(
        "--meter-folder",
        default=os.environ.get("HIFIMAGIC_METER_FOLDER", ""),
        help="Optional local audio folder used to derive peak-meter levels",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("cxn100")

    config_store = storage.ConfigStore()
    if args.meter_folder:
        try:
            folder = clean_meter_folder(args.meter_folder)
            config_store.set_meter_folder(folder)
            log.info("Using local audio folder %s for peak meters", folder)
        except (OSError, ValueError, storage.StorageError) as exc:
            log.warning("Could not set audio folder for peak meters: %s", exc)
    saved = config_store.load()
    device = args.host or saved.get("host") or ""
    device_port = args.device_port or saved.get("port") or 80
    if not device:
        found = discovery.discover_devices(timeout=2.5, max_workers=200)
        if found:
            device = str(found[0]["host"])
            device_port = int(found[0].get("port") or 80)
            try:
                config_store.save(device, device_port, found[0].get("name", ""))
            except (OSError, storage.StorageError) as exc:
                log.warning("Could not save discovered player: %s", exc)
            log.info("Discovered player at %s:%s", device, device_port)
        else:
            device = DEFAULT_DEVICE
            log.info("No saved or discovered player; showing the connection screen")

    server = None
    chosen_port = args.port
    for candidate in range(args.port, args.port + 20):
        try:
            server = build_server(
                args.bind, candidate, device, device_port, args.support_url, config_store
            )
            chosen_port = candidate
            break
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
    if server is None:
        raise SystemExit(f"No free web port found between {args.port} and {args.port + 19}")

    url = f"http://{args.bind if args.bind != '0.0.0.0' else '127.0.0.1'}:{chosen_port}/"
    if chosen_port != args.port:
        log.warning("Port %s was busy; using %s instead", args.port, chosen_port)
    log.info("Starting HifiMagic at %s (player %s:%s)", url, device, device_port)
    print(f"\n  HifiMagic controller running at {url}\n  Press Ctrl+C to quit.\n")
    print(
        f"  Player: {device}:{device_port}. "
        "Saved locally; use the splash screen in the browser to change it.\n"
    )

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        server.state.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

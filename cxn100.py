#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Launch the CXN100 / HifiMagic controller for Linux.

No third-party Python packages are required. Run this file with Python 3.9+
and open the printed URL in a browser, or let the script open it for you.
"""

from __future__ import annotations

import argparse
import logging
import threading
import webbrowser

from server import build_server

DEFAULT_DEVICE = "192.168.1.20"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="HifiMagic controller for Cambridge Audio network players"
    )
    parser.add_argument("--host", default=DEFAULT_DEVICE, help="IP address of the player (default: %(default)s)")
    parser.add_argument("--device-port", type=int, default=80, help="Player SMOIP port (default: %(default)s)")
    parser.add_argument("--port", type=int, default=8080, help="Local web server port (default: %(default)s)")
    parser.add_argument("--bind", default="127.0.0.1", help="Local bind address (default: %(default)s)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser window")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("cxn100")

    server = build_server(args.bind, args.port, args.host, args.device_port)

    url = f"http://{args.bind if args.bind != '0.0.0.0' else '127.0.0.1'}:{args.port}/"
    log.info("Starting HifiMagic at %s (player %s:%s)", url, args.host, args.device_port)
    print(f"\n  HifiMagic controller running at {url}\n  Press Ctrl+C to quit.\n")
    print(
        f"  Player: {args.host}:{args.device_port}. "
        "If it cannot be reached, use the splash screen in the browser to change it.\n"
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

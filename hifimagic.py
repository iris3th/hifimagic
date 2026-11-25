#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Launch HifiMagic Media Manager for Cambridge Audio StreamMagic network players.

No third-party Python packages are required. Run this file with Python 3.9+
and open the printed URL in a browser, or let the script open it for you.
"""

from __future__ import annotations

import argparse
import errno
import logging
import os
import shutil
import subprocess
import sys
import threading
import webbrowser

import discovery
import storage
from server import build_server

DEFAULT_DEVICE = "192.168.1.20"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="HifiMagic Media Manager for Cambridge Audio StreamMagic players"
    )
    parser.add_argument("--host", default=None, help="Player IP (default: saved config, then discovery)")
    parser.add_argument("--device-port", type=int, default=None, help="Player port (default: saved config, then 80)")
    parser.add_argument("--port", type=int, default=8765, help="Local web server port (default: %(default)s; next free port is used if busy)")
    parser.add_argument("--bind", default="127.0.0.1", help="Local bind address (default: %(default)s)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser window")
    parser.add_argument(
        "--browser-mode", choices=("app", "tab"), default="app",
        help="Open a dedicated app window when supported (default: %(default)s; 'tab' uses the default browser)",
    )
    parser.add_argument(
        "--support-url",
        default=os.environ.get("HIFIMAGIC_SUPPORT_URL", ""),
        help="Optional http(s) support link shown quietly in the footer",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args(argv)


def app_window_candidates(url: str) -> list[tuple[str, list[str]]]:
    """Known browsers that can open a dedicated, tab-free window."""
    app_args = ["--app=" + url, "--window-size=1280,820"]
    if sys.platform == "darwin":
        apps = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
        ]
        candidates = [(path, list(app_args)) for path in apps if os.path.exists(path)]
        firefox = "/Applications/Firefox.app/Contents/MacOS/firefox"
        if os.path.exists(firefox):
            candidates.append((firefox, ["--new-window", url]))
        return candidates
    if sys.platform == "win32":
        bases = [
            os.environ.get("PROGRAMFILES", ""),
            os.environ.get("PROGRAMFILES(X86)", ""),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        executables = [
            os.path.join("Google", "Chrome", "Application", "chrome.exe"),
            os.path.join("Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            os.path.join("Vivaldi", "Application", "vivaldi.exe"),
        ]
        candidates = []
        for base in bases:
            for executable in executables:
                path = os.path.join(base, executable)
                if base and os.path.exists(path):
                    candidates.append((path, list(app_args)))
        return candidates

    candidates = []
    for name in (
        "google-chrome-stable", "google-chrome", "chromium", "chromium-browser",
        "microsoft-edge", "brave-browser", "vivaldi",
    ):
        path = shutil.which(name)
        if path:
            candidates.append((path, list(app_args)))
    firefox = shutil.which("firefox")
    if firefox:
        candidates.append((firefox, ["--new-window", url]))
    return candidates


def open_ui(url: str, mode: str) -> bool:
    """Open the UI in an app window when possible; otherwise a browser tab."""
    log = logging.getLogger("hifimagic")
    if mode == "app":
        options: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if sys.platform != "win32":
            options["start_new_session"] = True
        for executable, args in app_window_candidates(url):
            try:
                subprocess.Popen([executable, *args], **options)
                return True
            except OSError as exc:
                log.debug("Could not launch %s: %s", executable, exc)
        log.warning("No dedicated app browser found; using the default browser instead")
    webbrowser.open(url)
    return False


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("hifimagic")

    config_store = storage.ConfigStore()
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
    log.info("Starting HifiMagic Media Manager at %s (player %s:%s)", url, device, device_port)
    print(f"\n  HifiMagic Media Manager running at {url}\n  Press Ctrl+C to quit.\n")
    print(
        f"  Active player: {device}:{device_port}. "
        "Other saved players can be selected in the browser; no credentials are stored.\n"
    )

    if not args.no_browser:
        def launch_browser() -> None:
            if not open_ui(url, args.browser_mode):
                print("  Opened the default browser; use --browser-mode app for a dedicated window.")
        threading.Timer(0.6, launch_browser).start()

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

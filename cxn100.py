#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backwards-compatible launcher for HifiMagic.

``hifimagic.py`` is the main entry point. This file remains so existing start
scripts and shortcuts from earlier releases keep working.
"""

from __future__ import annotations

import sys

from hifimagic import main

if __name__ == "__main__":
    sys.exit(main())

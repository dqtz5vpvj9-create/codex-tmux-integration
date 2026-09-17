#!/usr/bin/env python3
"""Compatibility entry point for the Codex session listener."""

import sys
from pathlib import Path

FEATURE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(FEATURE_ROOT))

from session_listener.cli import main
from session_listener.model import *  # noqa: F401,F403
from session_listener.protocol import *  # noqa: F401,F403


if __name__ == "__main__":
    raise SystemExit(main())

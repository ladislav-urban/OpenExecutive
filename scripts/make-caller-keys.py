#!/usr/bin/env python3
"""Make a key pair for signed callers (openexecutive/api/caller_keys.py).

    uv run --with cryptography python scripts/make-caller-keys.py [--erpnext | --erpnext-panel | ...]

The API image runs the same as `openexecutive make-caller-keys`, so a server
without a checkout makes its pairs where they are used.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "packages" / "core"))

from openexecutive.api.caller_keys import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

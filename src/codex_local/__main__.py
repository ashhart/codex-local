"""Allow ``python -m codex_local`` to launch the same entry point as ``codex-local``."""

from __future__ import annotations

import sys

from .launcher import main

if __name__ == "__main__":
    sys.exit(main())

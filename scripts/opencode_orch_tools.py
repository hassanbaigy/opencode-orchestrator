#!/usr/bin/env python3
"""Headless OpenCode launch, agent monitoring and run-journal validation tools.

Invoked by path from the plugin skills, so this stable entry point remains in
``scripts/``. Command coordination lives in ``opencode_orchestrator.cli``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opencode_orchestrator.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

"""Shared helpers for importing scripts/*.py modules inside tests.

The stage scripts live in a plain directory (no package). They import their
sibling ``_common`` module, so the scripts directory itself must be on
``sys.path`` before importing them.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def import_script(name: str):
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    return importlib.import_module(name)

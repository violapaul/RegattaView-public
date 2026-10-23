"""Pytest configuration for Libraries tests.

Adds the repo root to `sys.path` so `from Libraries import ...` works when
pytest is invoked from any working directory.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

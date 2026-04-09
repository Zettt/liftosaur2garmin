"""Vercel serverless entry point for liftosaur2garmin dashboard."""

import sys
from pathlib import Path

# Fallback: add src/ to path in case pip install didn't install the package
_src = str(Path(__file__).resolve().parent.parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from liftosaur2garmin.server import app  # noqa: F401

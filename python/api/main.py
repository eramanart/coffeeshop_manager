"""
api/main.py — thin shim for uvicorn compatibility.

All app logic lives in python/main.py.
Run with:
    uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from main import app  # noqa: F401 — re-exported for uvicorn

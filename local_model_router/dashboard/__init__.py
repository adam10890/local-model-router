"""Standalone dashboard: a single static page served at /ui."""
from __future__ import annotations

from pathlib import Path

_HTML_PATH = Path(__file__).resolve().parent / "index.html"


def dashboard_html() -> str:
    return _HTML_PATH.read_text(encoding="utf-8")

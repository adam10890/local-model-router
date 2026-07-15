"""Standalone dashboard: a single static page served at /ui."""
from __future__ import annotations

from pathlib import Path

from local_model_router import __version__

_HTML_PATH = Path(__file__).resolve().parent / "index.html"
_ICONS_PATH = Path(__file__).resolve().parent / "icons"


def dashboard_html(*, setup_token: str = "") -> str:
    html = _HTML_PATH.read_text(encoding="utf-8")
    return html.replace("__IMPERIUM_SETUP_TOKEN__", setup_token).replace(
        "__IMPERIUM_VERSION__", __version__
    )


def dashboard_icon(name: str) -> Path | None:
    if not name.endswith(".svg") or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-." for character in name):
        return None
    path = _ICONS_PATH / name
    return path if path.is_file() else None

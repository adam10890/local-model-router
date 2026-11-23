"""Standalone dashboard: static HTML shell and modular CSS/JS assets served at /ui."""
from __future__ import annotations

import html as _html
from pathlib import Path
import re

from local_model_router import __version__

_DASHBOARD_DIR = Path(__file__).resolve().parent
_HTML_PATH = _DASHBOARD_DIR / "index.html"
_ASSETS_PATH = _DASHBOARD_DIR / "assets"
_ICONS_PATH = _DASHBOARD_DIR / "icons"
_ASSET_NAME_RE = re.compile(r"^[a-z0-9-]+\.(css|js)$")
_ASSET_MEDIA_TYPES = {
    "css": "text/css; charset=utf-8",
    "js": "application/javascript; charset=utf-8",
}


def dashboard_html(*, setup_token: str = "") -> str:
    body = _HTML_PATH.read_text(encoding="utf-8")
    safe_token = _html.escape(setup_token, quote=True)
    return body.replace("__IMPERIUM_SETUP_TOKEN__", safe_token).replace(
        "__IMPERIUM_VERSION__", __version__
    )


def dashboard_asset(name: str) -> tuple[Path, str] | None:
    match = _ASSET_NAME_RE.fullmatch(name)
    if match is None:
        return None
    path = _ASSETS_PATH / name
    if not path.is_file():
        return None
    return path, _ASSET_MEDIA_TYPES[match.group(1)]


def dashboard_bundle_text(*, setup_token: str = "") -> str:
    parts = [dashboard_html(setup_token=setup_token)]
    for asset_name in ("styles.css", "i18n.js", "app.js"):
        resolved = dashboard_asset(asset_name)
        if resolved is not None:
            parts.append(resolved[0].read_text(encoding="utf-8"))
    return "\n".join(parts)


def dashboard_icon(name: str) -> Path | None:
    if not name.endswith(".svg") or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-." for character in name):
        return None
    path = _ICONS_PATH / name
    return path if path.is_file() else None

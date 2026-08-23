"""Hermetic dashboard contract for G6: i18n, theme, readiness states.

This is intentionally separate from the real-browser Playwright gate.
"""
from __future__ import annotations

from starlette.testclient import TestClient

from local_model_router.service.app import create_app


def _client(tmp_path, monkeypatch):
    monkeypatch.delenv("A0_LMM_ROUTER_API_KEY", raising=False)
    app = create_app(
        str(tmp_path / "missing.yaml"),
        setup_home=str(tmp_path / "home"),
        upstreams_path=str(tmp_path / "upstreams.yaml"),
        apps_path=str(tmp_path / "apps.yaml"),
        harnesses_path=str(tmp_path / "harnesses.yaml"),
    )
    return TestClient(app)


def test_dashboard_ships_en_he_and_light_dark_theme_hooks(tmp_path, monkeypatch):
    html = _client(tmp_path, monkeypatch).get("/ui").text

    assert 'lang="en"' in html
    assert 'data-theme="light"' in html
    assert 'html[data-theme="dark"]' in html
    assert 'id="theme-control"' in html
    assert 'id="mode-control"' not in html
    assert 'id="advanced-toggle"' in html
    assert '[dir="rtl"]' in html

    assert 'home:"Home"' in html and 'home:"בית"' in html
    assert 'chat:"Chat"' in html and 'chat:"צ׳אט"' in html
    assert 'models:"Models"' in html and 'models:"מודלים"' in html
    assert 'connections:"Connections"' in html and 'connections:"חיבורים"' in html
    assert 'light:"Light"' in html and 'dark:"Dark"' in html
    assert 'light:"בהיר"' in html and 'dark:"כהה"' in html
    assert "readyTitle:" in html and "attentionTitle:" in html
    assert "noInstalled:" in html
    assert "statusUnavailable:" in html or "statusUnavailable" in html
    assert 'if(route){closeDrawer();routeTo(route);return;}' in html
    assert "127.0.0.1:7440" not in html


def test_ui_status_exposes_actionable_readiness_fields(tmp_path, monkeypatch):
    body = _client(tmp_path, monkeypatch).get("/ui/status").json()

    assert body.get("overall")
    assert "next_action" in body
    assert "blocking_issues" in body
    assert isinstance(body["blocking_issues"], list)
    action = body["next_action"] or {}
    assert action.get("code") or action.get("label")
    assert "traceback" not in str(body).lower()

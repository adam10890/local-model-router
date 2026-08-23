from __future__ import annotations

import pytest


def _hash(page) -> str:
    return page.evaluate("location.hash")


def test_ready_navigation_uses_real_dashboard_without_browser_errors(dashboard):
    dashboard.set_ready()
    dashboard.goto()
    page = dashboard.page

    assert "Your local AI is ready" in page.locator("#main h1").inner_text()
    for route, heading in (
        ("chat", "Chat"),
        ("models", "Models"),
        ("connections", "Connections"),
        ("home", "Your local AI is ready"),
    ):
        page.locator(f'#simple-nav [data-route="{route}"]').click()
        assert _hash(page) == f"#/{route}"
        assert heading in page.locator("#main").inner_text()

    page.locator("#advanced-toggle").click()
    assert page.locator("#advanced-toggle").get_attribute("aria-expanded") == "true"
    page.locator('#advanced-nav [data-route="advanced/fleet"]').click()
    assert _hash(page) == "#/advanced/fleet"
    assert "Fleet" in page.locator("#main h1").inner_text()
    dashboard.assert_guards_clean()


def test_english_hebrew_and_theme_persist_after_reload(dashboard):
    dashboard.goto()
    page = dashboard.page
    html = page.locator("html")

    assert html.get_attribute("lang") == "en"
    assert html.get_attribute("dir") == "ltr"
    original_theme = html.get_attribute("data-theme")

    page.locator("#language-control").click()
    assert html.get_attribute("lang") == "he"
    assert html.get_attribute("dir") == "rtl"
    assert "ה־AI המקומי מוכן" in page.locator("#main h1").inner_text()

    page.locator("#theme-control").click()
    selected_theme = html.get_attribute("data-theme")
    assert selected_theme in {"light", "dark"}
    assert selected_theme != original_theme
    page.reload(wait_until="networkidle")
    page.locator("#main .page").wait_for(state="visible")

    assert html.get_attribute("lang") == "he"
    assert html.get_attribute("dir") == "rtl"
    assert html.get_attribute("data-theme") == selected_theme
    assert page.evaluate("localStorage.getItem('imperium.lang')") == "he"
    assert page.evaluate("localStorage.getItem('imperium.theme')") == selected_theme
    dashboard.assert_guards_clean()


@pytest.mark.parametrize("state", ["empty", "degraded", "error"])
def test_empty_degraded_and_error_states_are_actionable(dashboard, state):
    getattr(dashboard, f"set_{state}")()
    dashboard.goto()
    page = dashboard.page

    if state == "empty":
        assert "Let’s finish setup" in page.locator("#main h1").inner_text()
        assert page.locator("#alerts-count").is_hidden()
        assert page.locator('[data-route="setup"]').first.is_visible()
    elif state == "degraded":
        assert "needs attention" in page.locator("#main h1").inner_text().lower()
        assert page.locator("#alerts-count").inner_text() == "1"
        page.locator("#alerts-button").click()
        assert "server_stopped" in page.locator("#drawer-body").inner_text()
        page.locator('#drawer-body [data-route="advanced/fleet"]').click()
        assert _hash(page) == "#/advanced/fleet"
    else:
        assert page.locator("#alerts-count").inner_text() == "1"
        page.locator("#alerts-button").click()
        assert "could not load" in page.locator("#drawer-body").inner_text().lower()

    dashboard.assert_guards_clean()


def test_model_selection_and_hermes_pin_patch_success_and_failure(dashboard):
    dashboard.goto()
    page = dashboard.page

    page.locator('#simple-nav [data-route="models"]').click()
    page.locator('[data-model-tab="installed"]').click()
    page.locator('[data-action="choose-chat-model"][data-model="model-coder"]').click()
    assert _hash(page) == "#/chat"
    assert page.locator("#chat-model").input_value() == "model-coder"
    assert page.evaluate("localStorage.getItem('imperium.chatModel')") == "model-coder"

    page.locator('#simple-nav [data-route="connections"]').click()
    pin = page.locator("#hermes-pin-model")
    pin.wait_for(state="visible")
    pin.select_option("model-coder")
    page.locator('[data-action="pin-hermes"]').click()
    page.locator(".toast", has_text="Hermes pin updated").wait_for(state="visible")
    patch_calls = [call for call in dashboard.calls if call["method"] == "PATCH"]
    assert patch_calls[-1] == {
        "method": "PATCH",
        "path": "/harnesses/hermes/connections/default",
        "body": {"model": "model-coder"},
    }

    dashboard.patch_response.status = 500
    dashboard.patch_response.body = {"error": "pin_failed", "detail": "Pin update rejected"}
    dashboard.expect_http_failure()
    page.locator("#hermes-pin-model").select_option("model-chat")
    page.locator('[data-action="pin-hermes"]').click()
    page.locator(".toast", has_text="Pin update rejected").wait_for(state="visible")
    assert [call for call in dashboard.calls if call["method"] == "PATCH"][-1]["body"] == {
        "model": "model-chat"
    }
    dashboard.assert_guards_clean()


def test_alert_drawer_and_advanced_navigation_are_keyboard_operable(dashboard):
    dashboard.set_degraded()
    dashboard.goto()
    page = dashboard.page

    advanced = page.locator("#advanced-toggle")
    advanced.focus()
    assert page.evaluate("document.activeElement.id") == "advanced-toggle"
    advanced.press("Enter")
    assert advanced.get_attribute("aria-expanded") == "true"

    alerts = page.locator("#alerts-button")
    alerts.focus()
    alerts.press("Enter")
    assert page.locator("#drawer").get_attribute("class").endswith("open")
    assert page.locator("#drawer").get_attribute("aria-modal") == "true"
    page.keyboard.press("Escape")
    assert "open" not in page.locator("#drawer").get_attribute("class").split()
    dashboard.assert_guards_clean()


@pytest.mark.parametrize(
    "viewport",
    [
        {"width": 1440, "height": 1000},
        {"width": 390, "height": 844},
    ],
    ids=["desktop", "mobile"],
)
def test_ready_dashboard_has_no_overflow_and_basic_accessibility_faults(dashboard, viewport):
    dashboard.page.set_viewport_size(viewport)
    dashboard.goto()
    result = dashboard.page.evaluate(
        """
        () => {
          const visible = node => !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length);
          const ids = [...document.querySelectorAll('[id]')].map(node => node.id);
          const duplicates = [...new Set(ids.filter((id, index) => ids.indexOf(id) !== index))];
          const unnamedButtons = [...document.querySelectorAll('button')]
            .filter(visible)
            .filter(node => !(node.innerText.trim() || node.getAttribute('aria-label') || node.title))
            .map(node => node.id || node.outerHTML.slice(0, 80));
          const unlabeledControls = [...document.querySelectorAll('input, select, textarea')]
            .filter(visible)
            .filter(node => {
              const explicit = node.id && document.querySelector(`label[for="${CSS.escape(node.id)}"]`);
              return !(explicit || node.closest('label') || node.getAttribute('aria-label') || node.title);
            })
            .map(node => node.id || node.tagName);
          return {
            overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
            duplicates,
            unnamedButtons,
            unlabeledControls,
            imagesWithoutAlt: document.querySelectorAll('img:not([alt])').length,
            visibleH1: [...document.querySelectorAll('h1')].filter(visible).length,
          };
        }
        """
    )

    assert result == {
        "overflow": 0,
        "duplicates": [],
        "unnamedButtons": [],
        "unlabeledControls": [],
        "imagesWithoutAlt": 0,
        "visibleH1": 1,
    }
    dashboard.assert_guards_clean()


def test_local_storage_language_theme_and_chat_never_send_chat_or_foreign_request(dashboard):
    page = dashboard.page
    dashboard.goto()
    page.evaluate(
        """
        () => {
          localStorage.setItem('imperium.lang', 'he');
          localStorage.setItem('imperium.theme', 'dark');
          localStorage.setItem('imperium.chat', JSON.stringify([
            {role: 'user', content: '[redacted test message]'}
          ]));
        }
        """
    )
    dashboard.calls.clear()
    page.reload(wait_until="networkidle")
    page.locator('#simple-nav [data-route="chat"]').click()
    page.locator("#chat-messages").wait_for(state="visible")

    assert "[redacted test message]" in page.locator("#chat-messages").inner_text()
    assert page.locator("html").get_attribute("lang") == "he"
    assert page.locator("html").get_attribute("dir") == "rtl"
    assert page.locator("html").get_attribute("data-theme") == "dark"
    assert not [call for call in dashboard.calls if call["path"] == "/v1/chat/completions"]
    assert "[redacted test message]" not in str(dashboard.calls)
    dashboard.assert_guards_clean()

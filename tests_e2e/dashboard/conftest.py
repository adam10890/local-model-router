from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pytest


@dataclass
class ApiResponse:
    status: int
    body: object


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_server(url: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("dashboard test server did not start")


@pytest.fixture(scope="session")
def dashboard_server(tmp_path_factory):
    import uvicorn

    from local_model_router.service.agent_orchestrator import AgentOrchestrator
    from local_model_router.service.app import create_app
    from local_model_router.service.fleet_manager import FleetQueue, FleetStore

    root = tmp_path_factory.mktemp("dashboard-server")
    config = root / "llama_cpp_servers.yaml"
    config.write_text("global:\n  backend: remote\nactive_slots: []\n", encoding="utf-8")
    apps = root / "apps.yaml"
    apps.write_text("apps: {}\n", encoding="utf-8")
    harnesses = root / "harnesses.yaml"
    harnesses.write_text("harnesses: {}\n", encoding="utf-8")
    upstreams = root / "upstreams.yaml"
    upstreams.write_text("upstreams: {}\n", encoding="utf-8")
    store = FleetStore(str(root / "fleet.sqlite3"))
    orchestrator = AgentOrchestrator(
        db_path=str(root / "orchestrator.sqlite3"),
        workspace_root=str(root / "orchestrator"),
    )
    app = create_app(
        str(config),
        fleet_store=store,
        fleet_queue=FleetQueue(),
        upstreams_path=str(upstreams),
        apps_path=str(apps),
        harnesses_path=str(harnesses),
        orchestrator=orchestrator,
        setup_home=str(root / "imperium"),
        setup_api_enabled=False,
    )
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    _wait_for_server(url)
    yield url
    server.should_exit = True
    thread.join(timeout=10)
    if thread.is_alive():
        server.force_exit = True
        thread.join(timeout=5)


@pytest.fixture(scope="session")
def chromium_browser():
    try:
        from playwright import sync_api
    except ImportError as exc:
        pytest.fail(f"browser gate requires the browser extra: {exc}", pytrace=False)
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except sync_api.Error as exc:
            pytest.fail(f"browser gate requires Playwright Chromium: {exc}", pytrace=False)
        yield browser
        browser.close()


class Dashboard:
    API_PATHS = (
        "/ui/status",
        "/v1/models",
        "/harnesses",
        "/fleet/status",
        "/config/preview",
        "/cookbook",
        "/compute/budget",
        "/routing/evaluations",
        "/routing/request",
        "/v1/chat/completions",
    )

    def __init__(self, page, base_url: str):
        self.page = page
        self.base_url = base_url
        self.origin = urlsplit(base_url)
        self.calls: list[dict] = []
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.foreign_requests: list[str] = []
        self.expected_http_console_errors = 0
        self.patch_response = ApiResponse(200, {"ok": True})
        self.responses = self._ready_responses()
        self._observe_page()
        self._intercept_api()

    @staticmethod
    def _local_models() -> list[dict]:
        return [
            {
                "id": "model-chat",
                "name": "Qwen Chat",
                "path": "C:\\Models\\qwen-chat.gguf",
                "installed": True,
                "fit": "local",
                "fit_reason": "Safe local fit",
                "quant": "Q8_0",
                "size_gb": 1.7,
                "context": 4096,
                "license": "Apache-2.0",
                "capabilities": ["chat", "tools"],
            },
            {
                "id": "model-coder",
                "name": "Qwen Coder",
                "path": "C:\\Models\\qwen-coder.gguf",
                "installed": True,
                "fit": "local",
                "fit_reason": "Safe local fit",
                "quant": "Q8_0",
                "size_gb": 1.8,
                "context": 4096,
                "license": "Apache-2.0",
                "capabilities": ["chat", "code", "tools"],
            },
        ]

    def _setup(self, complete: bool = True) -> dict:
        models = self._local_models()
        return {
            "setup_complete": complete,
            "recommended_backend": "cpu",
            "platform_support": {"status": "supported"},
            "recommendation": models[0] if complete else None,
            "models": models if complete else [],
            "discovery": {
                "runtime_installed": complete,
                "config_exists": complete,
                "local_models": models if complete else [],
                "models_dir": "C:\\Models",
                "servers": [],
            },
            "hardware": {
                "system": {"os": "Windows", "release": "11"},
                "gpus": [{"name": "Test GPU", "memory_mb": 8192}],
                "ram": {"total_mb": 16384},
            },
        }

    def _ready_status(self) -> dict:
        return {
            "overall": "ready",
            "setup_api_active": False,
            "setup": self._setup(),
            "active": {"model": "model-chat"},
            "stages": [
                {"code": "system", "complete": True, "label": {"en": "System checked", "he": "המערכת נבדקה"}},
                {"code": "runtime", "complete": True, "label": {"en": "Runtime", "he": "Runtime"}},
                {"code": "model", "complete": True, "label": {"en": "Model", "he": "מודל"}},
                {"code": "server", "complete": True, "label": {"en": "Ready", "he": "מוכן"}},
            ],
            "blocking_issues": [],
            "optional_issues": [],
            "next_action": {"href": "#/chat", "label": {"en": "Open chat", "he": "פתיחת צ׳אט"}},
            "quick_actions": [
                {"href": "#/chat", "label": {"en": "Chat", "he": "צ׳אט"}},
                {"href": "#/models", "label": {"en": "Models", "he": "מודלים"}},
            ],
            "api": {"base_url": self.base_url},
        }

    def _ready_responses(self) -> dict[str, ApiResponse]:
        models = self._local_models()
        return {
            "/ui/status": ApiResponse(200, self._ready_status()),
            "/v1/models": ApiResponse(
                200,
                {
                    "data": [
                        {"id": "auto", "meta": {"kind": "alias", "maps_to_role": "auto"}},
                        {"id": "chat", "meta": {"kind": "alias", "maps_to_role": "chat"}},
                        {"id": "fast", "meta": {"kind": "alias", "maps_to_role": "utility"}},
                        {"id": "coder", "meta": {"kind": "alias", "maps_to_role": "coder"}},
                    ]
                },
            ),
            "/harnesses": ApiResponse(
                200,
                {
                    "config_writes_enabled": True,
                    "harnesses": [
                        {
                            "harness_id": "hermes",
                            "display_name": "Hermes",
                            "kind": "hermes",
                            "connections": [
                                {"name": "default", "model": "model-chat", "verification": {"state": "verified"}}
                            ],
                        }
                    ]
                },
            ),
            "/fleet/status": ApiResponse(
                200,
                {
                    "fleet_control": {"enabled": False, "supports_start_stop": False, "backend": "remote"},
                    "queues": {"local": {"mode": "bounded", "active": 0, "queued": 0, "max_active": 1, "max_queue": 4}},
                    "slots": [
                        {
                            "id": "slot_chat",
                            "role": "chat",
                            "model_id": "model-chat",
                            "model_path": models[0]["path"],
                            "backend_type": "remote",
                            "health": "healthy",
                            "runtime": {"running": True, "healthy": True},
                        },
                        {
                            "id": "slot_coder",
                            "role": "coder",
                            "model_id": "model-coder",
                            "model_path": models[1]["path"],
                            "backend_type": "remote",
                            "health": "healthy",
                            "runtime": {"running": True, "healthy": True},
                        },
                    ],
                },
            ),
            "/config/preview": ApiResponse(200, {"global": {"backend": "remote"}, "secrets": "<redacted>"}),
            "/cookbook": ApiResponse(200, {"models": [], "recommendations": {}}),
            "/compute/budget": ApiResponse(200, {"local": {"status": "ok"}, "providers": []}),
            "/routing/evaluations": ApiResponse(200, {"payload": {"models": []}}),
            "/routing/request": ApiResponse(200, {"selected_model": "model-chat", "reason_codes": ["local_healthy"]}),
            "/v1/chat/completions": ApiResponse(200, {"choices": [{"message": {"content": "OK"}}]}),
        }

    def set_ready(self) -> None:
        self.responses = self._ready_responses()

    def set_empty(self) -> None:
        self.set_ready()
        status = self._ready_status()
        status.update(
            {
                "overall": "setup_required",
                "setup_api_active": True,
                "setup": self._setup(complete=False),
                "active": {},
                "stages": [
                    {"code": "system", "complete": True, "label": {"en": "System checked", "he": "המערכת נבדקה"}},
                    {"code": "runtime", "complete": False, "label": {"en": "Runtime", "he": "Runtime"}},
                    {"code": "model", "complete": False, "label": {"en": "Model", "he": "מודל"}},
                ],
                "next_action": {"href": "#/setup", "label": {"en": "Continue setup", "he": "המשך ההגדרה"}},
                "quick_actions": [],
            }
        )
        self.responses["/ui/status"] = ApiResponse(200, status)
        self.responses["/v1/models"] = ApiResponse(200, {"data": []})
        self.responses["/harnesses"] = ApiResponse(200, {"harnesses": []})
        self.responses["/fleet/status"] = ApiResponse(200, {"slots": [], "queues": {}})

    def set_degraded(self) -> None:
        self.set_ready()
        status = self._ready_status()
        issue = {
            "code": "server_stopped",
            "category": "system",
            "severity": "blocking",
            "message": {"en": "The model server is stopped", "he": "שרת המודל נעצר"},
            "action": {"href": "#/advanced/fleet", "label": {"en": "Open Fleet", "he": "פתיחת Fleet"}},
        }
        status.update(
            {
                "overall": "needs_attention",
                "blocking_issues": [issue],
                "next_action": issue["action"],
            }
        )
        self.responses["/ui/status"] = ApiResponse(200, status)

    def set_error(self) -> None:
        self.set_ready()
        self.expect_http_failure()
        self.responses["/ui/status"] = ApiResponse(
            503,
            {"error": "status_unavailable", "detail": "Status service unavailable"},
        )

    def expect_http_failure(self) -> None:
        """Permit Chromium's own console line for one deliberately mocked HTTP failure."""
        self.expected_http_console_errors += 1

    def unexpected_console_errors(self) -> list[str]:
        remaining = self.expected_http_console_errors
        unexpected = []
        for message in self.console_errors:
            if remaining and message.startswith("Failed to load resource:"):
                remaining -= 1
            else:
                unexpected.append(message)
        return unexpected

    def _observe_page(self) -> None:
        self.page.on(
            "console",
            lambda message: self.console_errors.append(message.text) if message.type == "error" else None,
        )
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))

        def observe_request(request) -> None:
            parsed = urlsplit(request.url)
            if parsed.scheme in {"data", "blob", "about"}:
                return
            if (parsed.scheme, parsed.netloc) != (self.origin.scheme, self.origin.netloc):
                self.foreign_requests.append(request.url)

        self.page.on("request", observe_request)

    def _intercept_api(self) -> None:
        for path in self.API_PATHS:
            self.page.route(f"{self.base_url}{path}", self._handle_api)
        self.page.route(
            f"{self.base_url}/harnesses/*/connections/*",
            self._handle_api,
        )

    def _handle_api(self, route, request) -> None:
        parsed = urlsplit(request.url)
        body = request.post_data_json if request.post_data else None
        self.calls.append(
            {
                "method": request.method,
                "path": parsed.path,
                "body": body,
            }
        )
        if request.method == "PATCH" and parsed.path.startswith("/harnesses/"):
            response = self.patch_response
            if response.status < 400 and isinstance(body, dict):
                harnesses = self.responses["/harnesses"].body["harnesses"]
                harnesses[0]["connections"][0]["model"] = body.get("model")
        else:
            response = self.responses.get(
                parsed.path,
                ApiResponse(500, {"error": "unmocked_api", "detail": parsed.path}),
            )
        route.fulfill(
            status=response.status,
            content_type="application/json",
            body=json.dumps(response.body),
        )

    def goto(self, fragment: str = "") -> None:
        suffix = fragment if not fragment or fragment.startswith("#") else f"#{fragment}"
        self.page.goto(f"{self.base_url}/ui{suffix}", wait_until="networkidle")
        self.page.locator("#main .page").wait_for(state="visible")

    def assert_guards_clean(self) -> None:
        assert self.unexpected_console_errors() == []
        assert self.page_errors == []
        assert self.foreign_requests == []


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    setattr(item, f"rep_{call.when}", outcome.get_result())


@pytest.fixture
def dashboard(request, chromium_browser, dashboard_server):
    context = chromium_browser.new_context(viewport={"width": 1440, "height": 1000})
    context.tracing.start(screenshots=True, snapshots=True, sources=False)
    page = context.new_page()
    client = Dashboard(page, dashboard_server)
    yield client

    guard_errors = client.unexpected_console_errors() + client.page_errors + client.foreign_requests
    failed = bool(getattr(request.node, "rep_call", None) and request.node.rep_call.failed)
    if failed or guard_errors:
        artifact_root = Path(os.environ.get("PLAYWRIGHT_OUTPUT_DIR", "output/playwright")) / "dashboard"
        artifact_root.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", request.node.nodeid)[-140:]
        try:
            page.screenshot(path=str(artifact_root / f"{slug}.png"), full_page=True)
        finally:
            context.tracing.stop(path=str(artifact_root / f"{slug}.zip"))
    else:
        context.tracing.stop()
    context.close()
    if guard_errors and not failed:
        pytest.fail(f"browser guard failure: {guard_errors}")

"""End-to-end test against a *real* Playwright browser.

The pages are served by a tiny HTTP server started by this module, so the test
needs no internet access and still exercises real navigation, real form filling,
real proxy use and the real screenshot pipeline.

This is skipped automatically when Playwright or its browser binary is not
installed, so the fast offline suite is unaffected. The CI ``e2e`` job installs
chromium and runs it for real.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from app.core.engine import BrowserNotInstalledError, CaptureEngine
from app.core.settings import CaptureSettings

pytest.importorskip("playwright", reason="playwright package not installed")

INDEX_HTML = """<!doctype html>
<html><head><title>FullPage Capture Bot</title></head>
<body style="margin:0">
  <h1 style="font:48px sans-serif">FullPage Capture Bot</h1>
  <p style="font:20px sans-serif">A real page, served over real HTTP.</p>
</body></html>
"""

LOGIN_HTML = """<!doctype html>
<html><head><title>Sign in</title></head>
<body>
  <form method="post" action="/welcome">
    <input type="text" name="u">
    <input type="password" name="p">
    <button type="submit">Sign in</button>
  </form>
</body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    """Serves the fixture pages, and doubles as a forward proxy.

    A browser talking to an HTTP proxy asks for the *absolute* URL
    (``GET http://host/index.html HTTP/1.1``), which is what arrives in
    ``self.path`` below: answering that with the landing page is enough to prove
    that the real browser really went through the proxy.
    """

    pages = {"/index.html": INDEX_HTML, "/login.html": LOGIN_HTML}
    protocol_version = "HTTP/1.0"

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        path = str(self.path)
        if path.startswith(("http://", "https://")):
            body, status = INDEX_HTML.encode("utf-8"), 200  # a proxied request
        else:
            page = self.pages.get(path.split("?", 1)[0])
            body, status = (page.encode("utf-8"), 200) if page else (b"not found", 404)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # keep pytest's output clean
        return


@pytest.fixture(scope="module")
def site() -> str:
    """``http://127.0.0.1:<port>`` - the fixture pages, and a working proxy."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _settings(tmp_path) -> CaptureSettings:
    return CaptureSettings(
        output_dir=str(tmp_path),
        headless=True,
        scroll_to_load_lazy_content=False,
        network_idle_timeout_ms=1500,
        navigation_timeout_ms=20000,
    )


def _run_or_skip(settings: CaptureSettings, urls):
    """Run the real engine, skipping the test if the browser isn't installed."""
    engine = CaptureEngine(settings)
    try:
        return engine.run(urls)
    except BrowserNotInstalledError:
        pytest.skip("chromium browser binary not installed (run: playwright install chromium)")


def test_real_browser_captures_a_png(tmp_path, site):
    settings = _settings(tmp_path)
    summary = _run_or_skip(settings, [f"{site}/index.html"])

    assert summary.total == 1
    assert summary.succeeded == 1, summary.results[0].message

    files = sorted(tmp_path.glob("*.png"))
    assert files, "expected a screenshot file"
    with Image.open(files[0]) as image:
        # The screenshot is the viewport at the configured device scale, so the
        # pixel width is what the settings asked for - not a hard-coded number
        # that drifts the next time the defaults change.
        expected = round(settings.viewport_width * settings.effective_device_scale_factor)
        assert image.width == expected
        assert image.height > 0


def test_form_login_flow(tmp_path, site):
    settings = _settings(tmp_path)
    settings.auth_enabled = True
    settings.auth_mode = "form"
    settings.username = "demo"
    settings.password = "secret"
    summary = _run_or_skip(settings, [f"{site}/login.html"])
    assert summary.total == 1
    assert summary.succeeded == 1, summary.results[0].message


def test_har_recording(tmp_path, site):
    settings = _settings(tmp_path)
    settings.save_har = True
    summary = _run_or_skip(settings, [f"{site}/index.html"])
    assert summary.total == 1
    assert list(tmp_path.glob("*.har")), "expected a .har network log"


def test_proxy_config_is_accepted(tmp_path, site):
    # The fixture server answers absolute-form requests, so it *is* the proxy:
    # the host below never has to resolve, and a capture that succeeds proves
    # the real browser launched with - and used - the proxy configuration.
    settings = _settings(tmp_path)
    settings.proxy_server = site
    summary = _run_or_skip(settings, ["http://capture-bot.invalid/index.html"])
    assert summary.total == 1
    assert summary.succeeded == 1, summary.results[0].message


def test_webp_output_real(tmp_path, site):
    from PIL import features

    if not features.check("webp"):
        pytest.skip("Pillow built without WebP")
    settings = _settings(tmp_path)
    settings.image_format = "webp"
    summary = _run_or_skip(settings, [f"{site}/index.html"])
    assert summary.succeeded == 1
    files = list(tmp_path.glob("*.webp"))
    assert files
    with Image.open(files[0]) as image:
        assert image.format == "WEBP"

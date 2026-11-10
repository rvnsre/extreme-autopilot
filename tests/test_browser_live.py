import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from autopilot.executor import Executor
from autopilot.schema import Action

pytestmark = pytest.mark.skipif(os.environ.get("EA_TEST_BROWSER") != "1", reason="Opt-in real browser test")

HTML = b"""<!doctype html><title>Autopilot fixture</title><h1>Research source</h1>
<p>Python virtual environments isolate project dependencies.</p>
<label for="note">Note</label><input id="note"><button id="save"
onclick="document.querySelector('#result').textContent=document.querySelector('#note').value">Save</button>
<p id="result"></p>"""


def test_real_browser_elements_keyboard_freshness_and_screen(settings, tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings.browser_channel = os.environ.get("EA_TEST_BROWSER_CHANNEL") or None
    ex = Executor(settings, tmp_path / "browser-workspace", test_private_urls=True)
    brief = "Read the synthetic research page and prepare a local test note in its form."
    try:
        opened = ex.execute(
            brief,
            Action(
                tool="browser_open", intent="navigate", args={"url": f"http://127.0.0.1:{server.server_port}"}
            ),
        )
        assert opened.ok, opened.error
        assert "isolate project dependencies" in opened.data["text"]
        filled = ex.execute(
            brief,
            Action(
                tool="browser_fill",
                intent="local_change",
                authority_quote=brief,
                args={"selector": "#note", "text": "Verified note", "frame": opened.data["frame"]},
            ),
        )
        assert filled.ok, filled.error
        stale = ex.execute(
            brief,
            Action(
                tool="browser_focus",
                intent="navigate",
                args={"selector": "#save", "frame": opened.data["frame"]},
            ),
        )
        assert not stale.ok
        focused = ex.execute(
            brief,
            Action(
                tool="browser_focus",
                intent="navigate",
                args={"selector": "#save", "frame": filled.data["frame"]},
            ),
        )
        assert focused.ok, focused.error
        pressed = ex.execute(
            brief,
            Action(
                tool="browser_press",
                intent="local_change",
                authority_quote=brief,
                args={"key": "Enter", "frame": focused.data["frame"]},
            ),
        )
        assert pressed.ok, pressed.error
        assert "Verified note" in pressed.data["text"]
        visual = ex.execute(brief, Action(tool="browser_observe", args={"visual": True}))
        assert visual.ok and visual.data["screen"]["jpeg"]
        ex._page.locator("#note").focus()
        changed_focus = ex.execute(
            brief,
            Action(
                tool="browser_press",
                intent="local_change",
                authority_quote=brief,
                args={"key": "Enter", "frame": visual.data["frame"]},
            ),
        )
        assert not changed_focus.ok and not changed_focus.unknown
    finally:
        ex.close()
        server.shutdown()
        server.server_close()

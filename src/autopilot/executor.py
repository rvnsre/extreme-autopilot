"""Worker-only tools. Never instantiate on the coordinator or a personal desktop."""

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import create_model

from autopilot.config import Settings
from autopilot.desktop import capabilities, desktop_backend
from autopilot.policy import scoped_path, validate_authority, web_url
from autopilot.processes import kill_tree, managed_process
from autopilot.schema import Action, StrictModel, ToolResult

# Bounded, separately validated arguments; extra keys are never passed to libraries.
ARGUMENTS = {
    "observe": {},
    "browser_open": {"url": (str, ...)},
    "browser_observe": {"visual": (bool, False)},
    "browser_focus": {"selector": (str, ...), "frame": (str, ...)},
    "browser_fill": {"selector": (str, ...), "text": (str, ...), "frame": (str, ...)},
    "browser_press": {"key": (str, ...), "frame": (str, ...)},
    "browser_click": {"selector": (str, ""), "x": (int, -1), "y": (int, -1), "frame": (str, ...)},
    "browser_select": {"selector": (str, ...), "value": (str, ...), "frame": (str, ...)},
    "desktop_observe": {"handle": (int, 0), "visual": (bool, False)},
    "desktop_launch": {"app": (str, ...)},
    "desktop_focus": {"handle": (int, ...), "frame": (str, ...)},
    "desktop_type": {"text": (str, ...), "frame": (str, ...)},
    "desktop_key": {"keys": (list[str], ...), "frame": (str, ...)},
    "desktop_click": {"x": (int, ...), "y": (int, ...), "frame": (str, ...)},
    "file_read": {"path": (str, ...)},
    "file_write": {"path": (str, ...), "text": (str, ...)},
    "file_list": {"path": (str, ".")},
    "process": {"argv": (list[str], ...), "cwd": (str, "."), "timeout": (int, 600)},
    "http_get": {"url": (str, ...)},
    "api_request": {
        "url": (str, ...),
        "method": (str, "GET"),
        "json_body": (dict, {}),
        "credential": (str, ""),
    },
}
ARG_MODELS = {
    name: create_model(name + "Args", __base__=StrictModel, **fields) for name, fields in ARGUMENTS.items()
}


def tool_catalog():
    return {name: model.model_json_schema() for name, model in ARG_MODELS.items()}


def pack_image(image):
    image = image.convert("RGB")
    image.thumbnail((960, 640))
    stream = io.BytesIO()
    image.save(stream, "JPEG", quality=50)
    raw = stream.getvalue()
    if len(raw) > 256000:
        raise ValueError("Screenshot exceeds transport size limit")
    return {"jpeg": base64.b64encode(raw).decode(), "width": image.width, "height": image.height}


class Executor:
    def __init__(self, settings: Settings, root: Path, *, test_private_urls=False):
        self.settings, self.root = settings, root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.cancel = threading.Event()
        self._playwright = self._context = self._page = None
        self.browser_frame = self.desktop_frame = ""
        self.desktop_window = None
        self.desktop_handles = set()
        self._desktop_backend = None
        self.test_private_urls = test_private_urls

    def close(self):
        try:
            if self._context:
                self._context.close()
        finally:
            if self._playwright:
                self._playwright.stop()
            self._context = self._playwright = self._page = None

    def execute(self, prompt: str, action: Action):
        self._effect_started = False
        try:
            if self.cancel.is_set():
                return ToolResult(ok=False, error="Execution cancelled before input")
            args = ARG_MODELS[action.tool].model_validate(action.args).model_dump()
            if len(json.dumps(args).encode()) > 100000:
                raise ValueError("Tool arguments exceed 100 KB")
            validate_authority(prompt, action, self._page.url if self._page else "")
            data = self._dispatch(action.tool, args)
            if action.tool == "process" and data.get("interrupted"):
                return ToolResult(
                    ok=False,
                    data=data,
                    unknown=True,
                    error="Process interrupted; partial effects are unknown",
                )
            return ToolResult(ok=True, data=data)
        except Exception as exc:
            # Library errors can contain submitted text. Bounded output stays in private job storage.
            return ToolResult(
                ok=False, unknown=self._effect_started, error=f"{type(exc).__name__}: {str(exc)[:1500]}"
            )

    def _effect(self):
        if self.cancel.is_set():
            raise ValueError("Execution cancelled before input")
        self._effect_started = True

    def _browser(self):
        if self._page is None:
            from playwright.sync_api import sync_playwright

            profile = self.root / ".browser-profile"
            template = self.settings.browser_profile_template
            if template and not profile.exists():
                if not template.is_dir():
                    raise ValueError("Configured task-account browser profile template is missing")
                shutil.copytree(template, profile, ignore=shutil.ignore_patterns("Singleton*", "lockfile"))
            self._playwright = sync_playwright().start()
            try:
                self._context = self._playwright.chromium.launch_persistent_context(
                    str(profile),
                    headless=self.settings.headless,
                    channel=self.settings.browser_channel,
                    chromium_sandbox=True,
                    ignore_default_args=[
                        "--disable-client-side-phishing-detection",
                        "--unsafely-disable-devtools-self-xss-warnings",
                        "--enable-unsafe-swiftshader",
                    ],
                    viewport={"width": 1280, "height": 800},
                    accept_downloads=False,
                    permissions=[],
                    service_workers="block",
                )
            except BaseException:
                self._playwright.stop()
                self._playwright = None
                raise
            self._context.set_default_timeout(15000)
            self._context.set_default_navigation_timeout(30000)
            self._context.route("**/*", self._route)
            self._context.on("page", lambda page: page.on("dialog", lambda d: d.dismiss()))
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._page.on("dialog", lambda dialog: dialog.dismiss())
        return self._page

    def _route(self, route):
        try:
            web_url(route.request.url, self.test_private_urls)
            route.continue_()
        except ValueError:
            route.abort()

    def _browser_state(self, visual=False):
        page = self._browser()
        # Popup pages belong to this task's separate profile; observe before further action.
        pages = [p for p in self._context.pages if not p.is_closed()]
        if pages and pages[-1] != page:
            self._page = page = pages[-1]
        self.browser_frame = uuid.uuid4().hex
        text = page.locator("body").inner_text(timeout=10000)[:6000] if page.url != "about:blank" else ""
        controls = page.locator("body").aria_snapshot(timeout=10000)[:8000] if text else ""
        focus = page.evaluate("""() => { const e = document.activeElement;
            return e ? {tag:e.tagName,id:e.id,
            name:e.getAttribute('name'),type:e.getAttribute('type')} : null; }""")
        data = {
            "url": page.url,
            "title": page.title(),
            "text": text,
            "controls": controls,
            "frame": self.browser_frame,
            "focus": focus,
            "viewport": {"width": 1280, "height": 800},
        }
        self.browser_digest = hashlib.sha256(
            (page.url + text + controls + json.dumps(focus)).encode()
        ).hexdigest()
        if visual:
            from PIL import Image

            raw = page.screenshot(type="jpeg", quality=55)
            self.browser_visual_frame = self.browser_frame
            self.browser_image_digest = hashlib.sha256(raw).hexdigest()
            data["screen"] = pack_image(Image.open(io.BytesIO(raw)))
        return data

    def _check_browser(self, frame):
        if not self._page or frame != self.browser_frame or not frame:
            raise ValueError("Stale browser frame: observe again before input")
        page = self._page
        text = page.locator("body").inner_text()[:6000] if page.url != "about:blank" else ""
        controls = page.locator("body").aria_snapshot()[:8000] if text else ""
        focus = page.evaluate("""() => { const e = document.activeElement;
            return e ? {tag:e.tagName,id:e.id,
            name:e.getAttribute('name'),type:e.getAttribute('type')} : null; }""")
        digest = hashlib.sha256((page.url + text + controls + json.dumps(focus)).encode()).hexdigest()
        self.browser_frame = ""
        if digest != self.browser_digest:
            raise ValueError("Browser changed since observation; reobserve")
        return page

    def _desktop(self, handle=0, visual=False):
        backend = self._native_desktop()
        windows = backend.windows()[:40]
        self.desktop_handles = {w["handle"] for w in windows}
        self.desktop_frame = uuid.uuid4().hex
        data = {
            "windows": [{"handle": w["handle"], "title": w["title"]} for w in windows],
            "frame": self.desktop_frame,
            "backend": backend.name,
            "coordinate_units": "window-local OS logical coordinates; not resized JPEG pixels",
        }
        if handle:
            if handle not in self.desktop_handles:
                raise ValueError("Window handle not present in current desktop")
            w = next(w for w in windows if w["handle"] == handle)
            self.desktop_window = w
            data["handle"] = handle
            data["controls"] = backend.controls(handle)
            self.desktop_rect = tuple(w["rect"])
            data["bounds"] = {"width": w["rect"][2], "height": w["rect"][3]}
            if visual:
                image = backend.capture(w)
                self.desktop_visual_frame = self.desktop_frame
                self.desktop_image_digest = hashlib.sha256(image.tobytes()).hexdigest()
                data["screen"] = pack_image(image)
        else:
            self.desktop_window = None
        return data

    def _native_desktop(self):
        if self._desktop_backend is None:
            self._desktop_backend = desktop_backend()
        return self._desktop_backend

    def _check_desktop(self, frame):
        if not frame or frame != self.desktop_frame:
            raise ValueError("Stale desktop frame: observe again before input")
        self.desktop_frame = ""
        backend = self._native_desktop()
        current = next(
            (
                w
                for w in backend.windows()
                if self.desktop_window and w["handle"] == self.desktop_window["handle"]
            ),
            None,
        )
        if not current or not backend.focused(current):
            raise ValueError("Target window lost focus; observe and focus it again")
        if current["title"] != self.desktop_window["title"] or tuple(current["rect"]) != self.desktop_rect:
            raise ValueError("Target window changed; observe again before input")
        return self.desktop_window

    def _artifact(self, target):
        if not target.is_file() or target.stat().st_size > 512000:
            raise ValueError("Artifact must be a regular file no larger than 512 KB")
        raw = target.read_bytes()
        return {
            "path": str(target.relative_to(self.root)).replace("\\", "/"),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "content_b64": base64.b64encode(raw).decode(),
        }

    def _dispatch(self, tool, a):
        if tool == "observe":
            return {
                "files": self._dispatch("file_list", {"path": "."}),
                "browser": self._browser_state() if self._page else {"open": False},
                "capabilities": capabilities(),
                "note": "Recovery observation: previous uncertain side effects must not be repeated",
            }
        if tool == "browser_open":
            web_url(a["url"], self.test_private_urls)
            self._browser().goto(a["url"], wait_until="domcontentloaded")
            return self._browser_state()
        if tool == "browser_observe":
            return self._browser_state(a["visual"])
        if tool.startswith("browser_"):
            page = self._check_browser(a["frame"])
            if tool == "browser_press":
                if len(a["key"]) > 80:
                    raise ValueError("Key chord too long")
                self._effect()
                page.keyboard.press(a["key"])
            elif tool == "browser_click" and not a["selector"]:
                if a["frame"] != getattr(self, "browser_visual_frame", None):
                    raise ValueError("Coordinate input requires a fresh visual browser observation")
                if (
                    hashlib.sha256(page.screenshot(type="jpeg", quality=55)).hexdigest()
                    != self.browser_image_digest
                ):
                    raise ValueError("Browser pixels changed; reobserve before coordinate input")
                if not (0 <= a["x"] < 1280 and 0 <= a["y"] < 800):
                    raise ValueError("Coordinates must be in the reported viewport, not scaled JPEG pixels")
                self._effect()
                page.mouse.click(a["x"], a["y"])
            else:
                locator = page.locator(a["selector"])
                if locator.count() != 1:
                    raise ValueError("Selector must match exactly one element")
                if tool == "browser_focus":
                    locator.focus()
                elif tool == "browser_fill":
                    if not locator.is_editable():
                        raise ValueError("Target is not editable")
                    self._effect()
                    locator.fill(a["text"])
                elif tool == "browser_select":
                    if not locator.is_enabled():
                        raise ValueError("Target is disabled")
                    self._effect()
                    locator.select_option(a["value"])
                else:
                    if not locator.is_visible() or not locator.is_enabled():
                        raise ValueError("Target is not visible and enabled")
                    self._effect()
                    locator.click()
            return self._browser_state()
        if tool == "desktop_observe":
            return self._desktop(a["handle"], a["visual"])
        if tool == "desktop_launch":
            executable = self.settings.desktop_apps.get(a["app"])
            if not executable or not shutil.which(executable):
                raise ValueError("Application is not configured in EA_DESKTOP_APPS")
            self._effect()
            process = subprocess.Popen(
                [executable],
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return {
                "launched_app": a["app"],
                "pid": process.pid,
                "note": "Observe the desktop to discover the actual application window",
            }
        if tool == "desktop_focus":
            if a["frame"] != self.desktop_frame or a["handle"] not in self.desktop_handles:
                raise ValueError("Focus requires a current observed window handle")
            self._native_desktop().focus(a["handle"])
            return self._desktop(a["handle"], True)
        if tool.startswith("desktop_"):
            window = self._check_desktop(a["frame"])
            if tool == "desktop_type":
                if len(a["text"]) > 4000:
                    raise ValueError("Desktop typing is limited to 4,000 characters; prefer file/API tools")
                self._effect()
                self._native_desktop().type_text(window, a["text"])
            else:
                if tool == "desktop_key":
                    keys = a["keys"]
                    import re

                    if not 1 <= len(keys) <= 4 or any(not re.fullmatch(r"[a-z0-9]{1,12}", k) for k in keys):
                        raise ValueError("Use 1-4 lowercase key names, e.g. ctrl/s or command/s")
                    self._effect()
                    self._native_desktop().hotkey(keys)
                else:
                    if a["frame"] != getattr(self, "desktop_visual_frame", None):
                        raise ValueError("Coordinate input requires a fresh visual desktop observation")
                    if (
                        hashlib.sha256(self._native_desktop().capture(window).tobytes()).hexdigest()
                        != self.desktop_image_digest
                    ):
                        raise ValueError("Window pixels changed; reobserve before coordinate input")
                    left, top, width, height = self.desktop_rect
                    if not (0 <= a["x"] < width and 0 <= a["y"] < height):
                        raise ValueError("Coordinates must be inside the observed window in OS logical units")
                    self._effect()
                    self._native_desktop().click(left + a["x"], top + a["y"])
            return self._desktop(window["handle"], True)
        if tool.startswith("file_"):
            path = scoped_path(self.root, a["path"])
            if any(p.startswith(".browser") for p in path.relative_to(self.root).parts):
                raise ValueError("Browser credential profile is not a task artifact")
            if tool == "file_list":
                return {
                    "files": [
                        {"path": str(p.relative_to(self.root)).replace("\\", "/"), "directory": p.is_dir()}
                        for p in sorted(path.iterdir())[:100]
                        if not p.name.startswith(".")
                    ]
                }
            if tool == "file_write":
                raw = a["text"].encode()
                if len(raw) > 64000:
                    raise ValueError("Write at most 64 KB per file")
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
                try:
                    with temporary.open("xb") as output:
                        output.write(raw)
                        output.flush()
                        os.fsync(output.fileno())
                    self._effect()
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            artifact = self._artifact(path)
            raw = base64.b64decode(artifact["content_b64"])
            return {"artifact": artifact, "text": raw.decode("utf-8", errors="replace")[:10000]}
        if tool == "process":
            return self._process(a)
        if tool in {"http_get", "api_request"}:
            web_url(a["url"], self.test_private_urls)
            method = a.get("method", "GET")
            if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                raise ValueError("Unsupported API method")
            headers = {}
            credential = a.get("credential", "")
            if credential:
                profile = self.settings.api_credentials.get(credential)
                url = urlsplit(a["url"])
                if not profile or profile.origin.rstrip("/") != f"{url.scheme}://{url.netloc}":
                    raise ValueError("Credential profile must match the exact configured origin")
                if url.scheme != "https":
                    raise ValueError("Credentials require verified HTTPS")
                headers["Authorization"] = "Bearer " + profile.token.get_secret_value()
            with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
                kwargs = {"json": a.get("json_body", {})} if method != "GET" else {}
                if method != "GET":
                    self._effect()
                with client.stream(method, a["url"], headers=headers, **kwargs) as response:
                    response.raise_for_status()
                    if response.status_code >= 300:
                        raise ValueError("Redirects require a new explicit URL")
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 512000:
                            raise ValueError("HTTP response exceeds 512 KB")
            return {
                "url": a["url"],
                "status": response.status_code,
                "text": bytes(content).decode("utf-8", errors="replace")[:16000],
            }
        raise ValueError("Unsupported tool")

    def _process(self, a):
        argv = a["argv"]
        if not argv or len(argv) > 100 or any(len(x) > 64000 for x in argv):
            raise ValueError("Invalid process arguments")
        if argv[0] not in self.settings.process_commands:
            raise ValueError("Executable is not configured in EA_PROCESS_COMMANDS")
        executable = shutil.which(argv[0])
        if not executable or Path(executable).suffix.lower() in {".bat", ".cmd", ".ps1"}:
            raise ValueError(
                "Executable not found or requires a shell; use node npm-cli.js for npm on Windows"
            )
        if not 1 <= a["timeout"] <= 1800:
            raise ValueError("Process timeout must be between 1 and 1800 seconds")
        cwd = scoped_path(self.root, a["cwd"])
        # Commands are arbitrary code inside the VM, not a Python-level security sandbox.
        # Do not pass coordinator/worker/API credentials into child environments.
        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper()
            in {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "COMSPEC", "LANG", "SYSTEMDRIVE"}
        }
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "HOME": str(self.root)})
        captured, overflow, reader_stop = bytearray(), threading.Event(), threading.Event()
        total_read = 0
        with managed_process([executable, *argv[1:]], cwd, env, self._effect) as process:

            def drain():
                nonlocal total_read
                if os.name != "nt":
                    os.set_blocking(process.stdout.fileno(), False)
                while not reader_stop.is_set():
                    try:
                        chunk = os.read(process.stdout.fileno(), 4096)
                    except BlockingIOError:
                        time.sleep(0.01)
                        continue
                    if not chunk:
                        break
                    total_read += len(chunk)
                    if len(captured) < 64000:
                        captured.extend(chunk[: 64000 - len(captured)])
                    if total_read > 1048576:
                        overflow.set()

            thread = threading.Thread(target=drain, daemon=True)
            thread.start()
            deadline = time.monotonic() + a["timeout"]
            interrupted = False
            try:
                while process.poll() is None:
                    if self.cancel.is_set() or overflow.is_set() or time.monotonic() >= deadline:
                        interrupted = True
                        kill_tree(process)
                        break
                    time.sleep(0.1)
                process.wait(timeout=15)
                kill_tree(process)
            finally:
                try:
                    kill_tree(process)
                finally:
                    thread.join(timeout=5)
                    if thread.is_alive() and os.name != "nt":
                        interrupted = True
                        reader_stop.set()
                        thread.join(timeout=1)
            if thread.is_alive():
                raise RuntimeError("Process output did not close after tree termination")
            return {
                "argv": argv,
                "exit_code": process.returncode,
                "interrupted": interrupted,
                "output": captured.decode("utf-8", errors="replace"),
                "truncated": total_read > 64000 or interrupted,
            }

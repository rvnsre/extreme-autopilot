"""Lazy native desktop adapters. No OS modules or display connections on host import."""

import ctypes
import json
import os
import shutil
import subprocess
import sys


def command(argv):
    result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=15, check=True)
    if len(result.stdout.encode()) > 512000:
        raise ValueError("Desktop metadata exceeds 512 KB")
    return result.stdout.strip()


def default_apps(platform=None):
    platform = platform or sys.platform
    if platform == "win32":
        return {"notepad": "notepad.exe"}
    if platform == "darwin":
        return {"textedit": "/System/Applications/TextEdit.app/Contents/MacOS/TextEdit"}
    return {}  # Linux distributions ship different editors; explicitly configure one.


def default_data_dir(platform=None):
    from pathlib import Path

    platform = platform or sys.platform
    if platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "ExtremeAutopilot"
    if platform == "darwin":
        return Path.home() / "Library/Application Support/ExtremeAutopilot"
    base = os.environ.get("XDG_DATA_HOME", "")
    return (
        Path(base) if base and Path(base).is_absolute() else Path.home() / ".local/share"
    ) / "ExtremeAutopilot"


class WindowsDesktop:
    name = "windows-uia"

    def __init__(self):
        user32 = ctypes.windll.user32
        # Explicit pointer signatures avoid truncation on 64-bit Windows.
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        user32.CloseDesktop.argtypes = [ctypes.c_void_p]
        desktop = user32.OpenInputDesktop(0, False, 0x0100)
        if not desktop:
            raise RuntimeError("Interactive desktop unavailable or locked")
        user32.CloseDesktop(desktop)

    def window(self, handle):
        from pywinauto import Desktop

        return Desktop(backend="uia").window(handle=handle)

    def windows(self):
        from pywinauto import Desktop

        result = []
        for window in Desktop(backend="uia").windows(visible_only=True):
            title = window.window_text()
            if title and title != "Windows Default Lock Screen":
                rect = window.rectangle()
                result.append(
                    {
                        "handle": window.handle,
                        "title": title[:300],
                        "rect": (rect.left, rect.top, rect.width(), rect.height()),
                    }
                )
        return result

    def controls(self, handle):
        return [
            {"name": c.window_text()[:200], "type": c.element_info.control_type}
            for c in self.window(handle).descendants()[:150]
        ]

    def capture(self, window):
        return self.window(window["handle"]).capture_as_image()

    def focused(self, window):
        ctypes.windll.user32.GetForegroundWindow.restype = ctypes.c_void_p
        return ctypes.windll.user32.GetForegroundWindow() == window["handle"]

    def focus(self, handle):
        self.window(handle).set_focus()

    def type_text(self, window, text):
        escaped = "".join("{" + c + "}" if c in "+^%~(){}" else c for c in text)
        self.window(window["handle"]).type_keys(
            escaped, with_spaces=True, with_tabs=True, with_newlines=True, pause=0.005, vk_packet=True
        )

    def hotkey(self, keys):
        import pyautogui

        if any(k not in pyautogui.KEYBOARD_KEYS for k in keys):
            raise ValueError("Unknown Windows key name")
        pyautogui.FAILSAFE = True
        pyautogui.hotkey(*keys)

    def click(self, x, y):
        import pyautogui

        pyautogui.FAILSAFE = True
        pyautogui.click(x, y)


class LinuxDesktop:
    name = "linux-x11"

    def __init__(self):
        if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            raise RuntimeError("Native Wayland desktop input is unsupported; use an isolated X11 session")
        if not os.environ.get("DISPLAY"):
            raise RuntimeError("Linux desktop requires an unlocked X11 DISPLAY")
        for executable in ("wmctrl", "xdotool"):
            if not shutil.which(executable):
                raise RuntimeError(f"Linux desktop requires {executable}")
        session = os.environ.get("XDG_SESSION_ID")
        if session and shutil.which("loginctl"):
            if command(["loginctl", "show-session", session, "--property=LockedHint", "--value"]) != "no":
                raise RuntimeError("Linux session is locked or its lock state is unavailable")

    def windows(self):
        result = []
        for line in command(["wmctrl", "-lpG"]).splitlines():
            parts = line.split(None, 8)
            if len(parts) == 9:
                handle, _, _, x, y, width, height, _, title = parts
                rect = tuple(map(int, (x, y, width, height)))
                if rect[2] > 0 and rect[3] > 0:
                    result.append({"handle": int(handle, 16), "title": title[:300], "rect": rect})
        return result

    def controls(self, handle):
        return []  # X11 window metadata is not an accessibility tree. Prefer browser elements.

    def capture(self, window):
        from PIL import ImageGrab

        x, y, width, height = window["rect"]
        return ImageGrab.grab(bbox=(x, y, x + width, y + height), xdisplay=os.environ["DISPLAY"])

    def focused(self, window):
        return int(command(["xdotool", "getactivewindow"])) == window["handle"]

    def focus(self, handle):
        command(["xdotool", "windowactivate", "--sync", str(handle)])

    def type_text(self, window, text):
        # xdotool maps missing Unicode keysyms; stdin prevents text becoming command options.
        subprocess.run(
            ["xdotool", "type", "--clearmodifiers", "--delay", "5", "--file", "-"],
            input=text,
            encoding="utf-8",
            capture_output=True,
            timeout=60,
            check=True,
        )

    def hotkey(self, keys):
        aliases = {
            "enter": "Return",
            "esc": "Escape",
            "backspace": "BackSpace",
            "delete": "Delete",
            "pageup": "Prior",
            "pagedown": "Next",
            "win": "super",
            "command": "super",
            "left": "Left",
            "right": "Right",
            "up": "Up",
            "down": "Down",
            "tab": "Tab",
            "home": "Home",
            "end": "End",
            "space": "space",
        }
        command(["xdotool", "key", "--clearmodifiers", "+".join(aliases.get(k, k) for k in keys)])

    def click(self, x, y):
        command(["xdotool", "mousemove", "--sync", str(int(x)), str(int(y)), "click", "1"])


# Fixed program. App IDs, text and window titles are always data, never interpolated code.
MAC_SCRIPT = r"""
function run(argv) {
    const se = Application('System Events');
    const op = argv[0];
    if (op === 'windows') {
        const result = [];
        const ps = se.applicationProcesses.whose({backgroundOnly:false})();
        for (let i=0; i<ps.length && result.length<40; i++) {
            try {
                const p=ps[i], w=p.windows[0];
                const pos=w.position(), size=w.size();
                result.push({handle:p.unixId(), title:w.name().slice(0,300),
                    rect:[pos[0],pos[1],size[0],size[1]]});
            } catch (_) {} // An app may have no window or may have just exited.
        }
        return JSON.stringify(result);
    }
    const p=se.applicationProcesses.whose({unixId:Number(argv[1])})[0];
    if (op === 'focus') { p.frontmost=true; p.windows[0].actions.byName('AXRaise').perform(); return 'ok'; }
    if (op === 'focused') { return JSON.stringify(p.frontmost()); }
    if (op === 'controls') {
        const result=[], es=p.windows[0].uiElements();
        for(let i=0;i<es.length && i<150;i++) {
            try { result.push({name:String(es[i].name() || '').slice(0,200),type:es[i].role()}); }
            catch (_) {}
        }
        return JSON.stringify(result);
    }
    throw Error('Unsupported desktop operation');
}
"""


class MacDesktop:
    name = "macos-accessibility"

    def __init__(self):
        self.cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        ax.AXIsProcessTrusted.restype = ctypes.c_bool
        self.cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        if not ax.AXIsProcessTrusted() or not self.cg.CGPreflightScreenCaptureAccess():
            raise RuntimeError(
                "Grant Accessibility and Screen Recording to the worker during setup, then restart"
            )
        # Preflight without asking: missing Apple Events permission must never open a task-time dialog.
        ae = ctypes.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")

        class Desc(ctypes.Structure):
            _fields_ = [("type", ctypes.c_uint32), ("data", ctypes.c_void_p)]

        desc = Desc()
        ae.AECreateDesc.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_long, ctypes.POINTER(Desc)]
        ae.AEDeterminePermissionToAutomateTarget.argtypes = [
            ctypes.POINTER(Desc),
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_bool,
        ]
        ae.AEDisposeDesc.argtypes = [ctypes.POINTER(Desc)]
        bundle = b"com.apple.systemevents"
        status = ae.AECreateDesc(int.from_bytes(b"bund", "big"), bundle, len(bundle), ctypes.byref(desc))
        if status:
            raise RuntimeError("Cannot preflight macOS Automation permission")
        try:
            wildcard = int.from_bytes(b"****", "big")
            if ae.AEDeterminePermissionToAutomateTarget(ctypes.byref(desc), wildcard, wildcard, False):
                raise RuntimeError("Grant Automation access to System Events during initial setup")
        finally:
            ae.AEDisposeDesc(ctypes.byref(desc))

    def script(self, op, handle=0):
        return command(["/usr/bin/osascript", "-l", "JavaScript", "-e", MAC_SCRIPT, op, str(handle)])

    def windows(self):
        return json.loads(self.script("windows"))

    def controls(self, handle):
        return json.loads(self.script("controls", handle))

    def capture(self, window):
        from PIL import ImageGrab

        x, y, width, height = window["rect"]
        return ImageGrab.grab(bbox=(x, y, x + width, y + height))

    def focused(self, window):
        return json.loads(self.script("focused", window["handle"])) is True

    def focus(self, handle):
        self.script("focus", handle)

    def type_text(self, window, text):
        # Native UTF-16 events preserve Unicode and do not replace clipboard contents.
        cg = self.cg
        cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        cg.CGEventKeyboardSetUnicodeString.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
        cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        for char in text:
            raw = char.encode("utf-16-le")
            units = (ctypes.c_uint16 * (len(raw) // 2)).from_buffer_copy(raw)
            for down in (True, False):
                event = cg.CGEventCreateKeyboardEvent(None, 0, down)
                if not event:
                    raise RuntimeError("Unable to create Unicode input event")
                try:
                    cg.CGEventKeyboardSetUnicodeString(event, len(units), units)
                    cg.CGEventPost(0, event)
                finally:
                    cf.CFRelease(event)

    def hotkey(self, keys):
        # Public macOS virtual key codes; modifier state belongs to each event, not held global keys.
        codes = {
            "a": 0,
            "s": 1,
            "d": 2,
            "f": 3,
            "h": 4,
            "g": 5,
            "z": 6,
            "x": 7,
            "c": 8,
            "v": 9,
            "b": 11,
            "q": 12,
            "w": 13,
            "e": 14,
            "r": 15,
            "y": 16,
            "t": 17,
            "1": 18,
            "2": 19,
            "3": 20,
            "4": 21,
            "6": 22,
            "5": 23,
            "9": 25,
            "7": 26,
            "8": 28,
            "0": 29,
            "o": 31,
            "u": 32,
            "i": 34,
            "p": 35,
            "l": 37,
            "j": 38,
            "k": 40,
            "n": 45,
            "m": 46,
        }
        codes.update(
            enter=36,
            tab=48,
            space=49,
            backspace=51,
            esc=53,
            delete=117,
            home=115,
            end=119,
            pageup=116,
            pagedown=121,
            left=123,
            right=124,
            down=125,
            up=126,
        )
        modifiers = {"command": 1 << 20, "win": 1 << 20, "ctrl": 1 << 18, "alt": 1 << 19, "shift": 1 << 17}
        normal = [k for k in keys if k not in modifiers]
        if len(normal) != 1 or normal[0] not in codes:
            raise ValueError("macOS hotkeys require modifiers and one supported key")
        flags = 0
        for k in keys:
            flags |= modifiers.get(k, 0)
        cg = self.cg
        cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        for down in (True, False):
            event = cg.CGEventCreateKeyboardEvent(None, codes[normal[0]], down)
            if not event:
                raise RuntimeError("Unable to create keyboard event")
            try:
                cg.CGEventSetFlags(event, flags)
                cg.CGEventPost(0, event)
            finally:
                cf.CFRelease(event)

    def click(self, x, y):
        class Point(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

        cg = self.cg
        cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, Point, ctypes.c_uint32]
        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        for kind in (5, 1, 2):  # move, left down, left up
            event = cg.CGEventCreateMouseEvent(None, kind, Point(x, y), 0)
            if not event:
                raise RuntimeError("Unable to create mouse event")
            try:
                cg.CGEventPost(0, event)
            finally:
                cf.CFRelease(event)


def desktop_backend():
    if sys.platform == "win32":
        return WindowsDesktop()
    if sys.platform == "linux":
        return LinuxDesktop()
    if sys.platform == "darwin":
        return MacDesktop()
    raise RuntimeError(f"Unsupported desktop platform: {sys.platform}")


def capabilities(probe=False):
    result = {
        "platform": sys.platform,
        "browser": True,
        "files": True,
        "process": True,
        "desktop_backend": {
            "win32": "windows-uia",
            "linux": "linux-x11",
            "darwin": "macos-accessibility",
        }.get(sys.platform),
        "desktop_ready": None,
    }
    if probe:
        try:
            backend = desktop_backend()
            result.update(desktop_ready=True, desktop_windows=len(backend.windows()))
        except Exception as exc:
            result.update(desktop_ready=False, desktop_error=str(exc)[:500])
    return result

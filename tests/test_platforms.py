import sys

import pytest
from PIL import Image

from autopilot import desktop
from autopilot.executor import Executor
from autopilot.schema import Action


@pytest.mark.parametrize("platform,name", [("win32", "notepad"), ("darwin", "textedit")])
def test_platform_default_apps(platform, name):
    assert name in desktop.default_apps(platform)
    assert desktop.default_apps("linux") == {}


def test_data_directories(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "custom-data"))
    assert desktop.default_data_dir("linux") == tmp_path / "custom-data/ExtremeAutopilot"
    assert "Application Support" in str(desktop.default_data_dir("darwin"))
    monkeypatch.setenv("XDG_DATA_HOME", "relative")
    assert ".local" in str(desktop.default_data_dir("linux"))


@pytest.mark.parametrize(
    "variable,value", [("WAYLAND_DISPLAY", "wayland-0"), ("XDG_SESSION_TYPE", "wayland")]
)
def test_wayland_is_rejected_even_with_xwayland_display(monkeypatch, variable, value):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv(variable, value)
    with pytest.raises(RuntimeError, match="Wayland"):
        desktop.LinuxDesktop()


def test_missing_display_is_rejected(monkeypatch):
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="DISPLAY"):
        desktop.LinuxDesktop()


def test_linux_window_parser_and_input_use_arguments(monkeypatch):
    backend = object.__new__(desktop.LinuxDesktop)
    calls = []

    def fake(argv):
        calls.append(argv)
        if argv == ["wmctrl", "-lpG"]:
            return "0x00800007  0 123  10 20 800 600 host My editor — sənəd\ninvalid"
        return "8388615"

    monkeypatch.setattr(desktop, "command", fake)
    windows = backend.windows()
    assert windows == [{"handle": 8388615, "title": "My editor — sənəd", "rect": (10, 20, 800, 600)}]
    assert backend.focused(windows[0])
    backend.hotkey(["ctrl", "enter"])
    assert calls[-1] == ["xdotool", "key", "--clearmodifiers", "ctrl+Return"]


class FixtureDesktop:
    name = "fixture"

    def __init__(self):
        self.title = "Editor"
        self.rect = (10, 20, 800, 600)
        self.active = True
        self.inputs = []

    def windows(self):
        return [{"handle": 123, "title": self.title, "rect": self.rect}]

    def focused(self, window):
        return self.active

    def controls(self, handle):
        return [{"name": "Document", "type": "text"}]

    def capture(self, window):
        return Image.new("RGB", (800, 600))

    def type_text(self, window, text):
        self.inputs.append(text)


@pytest.mark.parametrize("change", ["title", "rect", "active"])
def test_desktop_input_fences_changed_window(settings, tmp_path, change):
    executor = Executor(settings, tmp_path)
    backend = executor._desktop_backend = FixtureDesktop()
    state = executor._desktop(123, True)
    setattr(backend, change, {"title": "Other document", "rect": (11, 20, 800, 600), "active": False}[change])
    result = executor.execute(
        "Type text in the editor.",
        Action(
            tool="desktop_type",
            intent="local_change",
            authority_quote="Type text in the editor.",
            args={"frame": state["frame"], "text": "secret"},
        ),
    )
    assert not result.ok and not result.unknown and not backend.inputs


def test_unicode_typing_uses_native_adapter(settings, tmp_path):
    executor = Executor(settings, tmp_path)
    backend = executor._desktop_backend = FixtureDesktop()
    state = executor._desktop(123, True)
    prompt = "Type text in the editor."
    text = "Azərbaycan Русский 😀"
    result = executor.execute(
        prompt,
        Action(
            tool="desktop_type",
            intent="local_change",
            authority_quote=prompt,
            args={"frame": state["frame"], "text": text},
        ),
    )
    assert result.ok and backend.inputs == [text]
    assert result.data["backend"] == "fixture"


def test_capability_probe_reports_missing_permissions(monkeypatch):
    def denied():
        raise RuntimeError("Permission missing")

    monkeypatch.setattr(desktop, "desktop_backend", denied)
    assert desktop.capabilities()["desktop_ready"] is None
    assert desktop.capabilities(probe=True)["desktop_ready"] is False
    assert desktop.capabilities(probe=True)["desktop_error"] == "Permission missing"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS native permission preflight")
def test_real_macos_permission_preflight_does_not_request_access():
    result = desktop.capabilities(probe=True)
    assert result["desktop_backend"] == "macos-accessibility"
    assert isinstance(result["desktop_ready"], bool)
    if not result["desktop_ready"]:
        assert result["desktop_error"]

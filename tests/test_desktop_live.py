"""Real X11 window/input/capture integration, synthetic text and a disposable process only."""

import os
import subprocess
import sys
import time

import pytest

from autopilot.executor import Executor
from autopilot.schema import Action

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("EA_TEST_DESKTOP") != "1", reason="Opt-in X11 integration"
)


def test_real_x11_unicode_input_and_readback(settings, tmp_path):
    output = tmp_path / "desktop-result.txt"
    script = (
        "import sys,time,pathlib; "
        "pathlib.Path(sys.argv[1]).write_text(input(),encoding='utf-8'); time.sleep(20)"
    )
    process = subprocess.Popen(
        ["xterm", "-u8", "-T", "Autopilot fixture", "-e", sys.executable, "-c", script, str(output)]
    )
    executor = Executor(settings, tmp_path)
    prompt = "Type the requested text into the test application and press Enter."
    try:
        deadline = time.monotonic() + 15
        handle = None
        while time.monotonic() < deadline:
            state = executor._desktop()
            handle = next((w["handle"] for w in state["windows"] if w["title"] == "Autopilot fixture"), None)
            if handle:
                break
            time.sleep(0.1)
        assert handle, "Xterm did not appear in the real X11 window list"
        state = executor._dispatch("desktop_focus", {"handle": handle, "frame": state["frame"]})
        assert state["screen"]["jpeg"] and state["bounds"]["width"] > 0
        text = "Salam Azərbaycan — русский"
        result = executor.execute(
            prompt,
            Action(
                tool="desktop_type",
                intent="local_change",
                authority_quote=prompt,
                args={"text": text, "frame": state["frame"]},
            ),
        )
        assert result.ok, result.error
        result = executor.execute(
            prompt,
            Action(
                tool="desktop_key",
                intent="local_change",
                authority_quote=prompt,
                args={"keys": ["enter"], "frame": result.data["frame"]},
            ),
        )
        assert result.ok, result.error
        deadline = time.monotonic() + 5
        while not output.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert output.read_text(encoding="utf-8") == text
        stale = executor.execute(
            prompt,
            Action(
                tool="desktop_type",
                intent="local_change",
                authority_quote=prompt,
                args={"text": "should not type", "frame": state["frame"]},
            ),
        )
        assert not stale.ok and not stale.unknown
    finally:
        executor.close()
        process.terminate()
        process.wait(timeout=10)

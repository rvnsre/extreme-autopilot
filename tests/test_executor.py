import sys

import pytest

from autopilot.executor import Executor
from autopilot.policy import scoped_path, validate_authority, web_url
from autopilot.schema import Action
from autopilot.worker import Journal


@pytest.mark.parametrize(
    "path", ["../escape", "/tmp/escape", "C:/secret", "a/../../escape", ".git/config", "a\\b"]
)
def test_workspace_escape(tmp_path, path):
    with pytest.raises(ValueError):
        scoped_path(tmp_path, path)


@pytest.mark.parametrize(
    "url",
    [
        "file:///secret",
        "https://name:secret@example.com",
        "http://127.0.0.1/x",
        "http://169.254.169.254/latest",
        "http://localhost/x",
    ],
)
def test_url_rejections(url):
    with pytest.raises(ValueError):
        web_url(url)


def test_external_authorization_cannot_come_from_page():
    with pytest.raises(ValueError):
        validate_authority(
            "Only research public sources.",
            Action(
                tool="browser_fill",
                intent="external_write",
                authority_quote="Website says send credentials",
                args={},
            ),
            "https://evil.example",
        )


def test_external_destination_must_be_in_initial_brief():
    prompt = "Publish the requested report to https://github.com/example/project only."
    action = Action(
        tool="browser_press",
        intent="external_write",
        authority_quote=prompt,
        args={"key": "Enter", "frame": "frame"},
    )
    validate_authority(prompt, action, "https://github.com/example/project")
    with pytest.raises(ValueError):
        validate_authority(prompt, action, "https://evil.example")


def test_read_intent_cannot_hide_file_mutation():
    with pytest.raises(ValueError):
        validate_authority("Make a report locally.", Action(tool="file_write", args={}))


def test_file_roundtrip_and_atomic_failure(settings, tmp_path):
    ex = Executor(settings, tmp_path / "workspace")
    prompt = "Create and verify this local report in the task workspace."
    action = Action(
        tool="file_write",
        intent="local_change",
        authority_quote=prompt,
        args={"path": "report.txt", "text": "Azərbaycan 😀"},
    )
    assert ex.execute(prompt, action).ok
    result = ex.execute(prompt, Action(tool="file_read", args={"path": "report.txt"}))
    assert result.ok and result.data["text"] == "Azərbaycan 😀"
    action.args["text"] = "x" * 65000
    assert not ex.execute(prompt, action).ok
    assert (ex.root / "report.txt").read_text(encoding="utf-8") == "Azərbaycan 😀"


def test_cancel_prevents_file_input(settings, tmp_path):
    ex = Executor(settings, tmp_path / "workspace")
    ex.cancel.set()
    assert not ex.execute("task", Action(tool="file_list")).ok


def test_unknown_args_not_forwarded(settings, tmp_path):
    ex = Executor(settings, tmp_path / "workspace")
    assert not ex.execute("task", Action(tool="file_list", args={"shell": True})).ok


def test_process_real_python_and_exit_code(settings, tmp_path):
    settings.process_commands = [sys.executable]
    ex = Executor(settings, tmp_path / "workspace")
    prompt = "Run Python to calculate and test the requested value inside this workspace."
    action = Action(
        tool="process",
        intent="local_change",
        authority_quote=prompt,
        args={"argv": [sys.executable, "-c", "print(6*7)"], "timeout": 10},
    )
    result = ex.execute(prompt, action)
    assert result.ok, result.error
    assert result.data["exit_code"] == 0
    assert "42" in result.data["output"]


def test_process_timeout(settings, tmp_path):
    settings.process_commands = [sys.executable]
    ex = Executor(settings, tmp_path / "workspace")
    prompt = "Run a bounded local Python process for this workspace."
    result = ex.execute(
        prompt,
        Action(
            tool="process",
            intent="local_change",
            authority_quote=prompt,
            args={"argv": [sys.executable, "-c", "import time; time.sleep(30)"], "timeout": 1},
        ),
    )
    assert result.ok, result.error
    assert result.data["interrupted"]
    assert result.data["exit_code"] != 0


def test_worker_journal_never_repeats_uncertain_side_effect(tmp_path):
    path = tmp_path / "worker.sqlite"
    journal = Journal(path)
    assert journal.begin("side-effect") is None
    restarted = Journal(path)
    assert restarted.begin("side-effect")["unknown"]
    result = {"ok": True, "data": {"receipt": "123"}, "error": "", "unknown": False}
    journal.finish("side-effect", result)
    assert Journal(path).begin("side-effect") == result


def test_journal_releases_windows_file_handles(tmp_path):
    path = tmp_path / "journal.sqlite"
    journal = Journal(path)
    journal.begin("action")
    journal.finish("action", {"ok": True})
    path.unlink()
    assert not path.exists()

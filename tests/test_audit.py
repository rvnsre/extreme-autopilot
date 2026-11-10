"""Regression cases for concrete v0.1.0 audit failures; fixtures never contact a paid model."""

import asyncio
import os
import sys
import time

import pytest
from test_engine import FixtureModels, sample_plan

from autopilot.engine import Engine, compact
from autopilot.executor import Executor
from autopilot.models import ModelUnavailable
from autopilot.policy import validate_authority
from autopilot.schema import Action, Continue, Decision, Finish, Verdict
from autopilot.store import encode


@pytest.mark.parametrize("tool", ["file_write", "process", "desktop_type"])
def test_navigation_cannot_bypass_write_authority(tool):
    with pytest.raises(ValueError):
        validate_authority("Research this task.", Action(tool=tool, intent="navigate"))


def test_failed_readback_after_real_write_is_unknown(settings, tmp_path, monkeypatch):
    executor = Executor(settings, tmp_path / "workspace")
    prompt = "Write the requested local report in this workspace."
    monkeypatch.setattr(executor, "_artifact", lambda path: (_ for _ in ()).throw(OSError("readback failed")))
    result = executor.execute(
        prompt,
        Action(
            tool="file_write",
            intent="local_change",
            authority_quote=prompt,
            args={"path": "report.txt", "text": "actually written"},
        ),
    )
    assert (executor.root / "report.txt").read_text() == "actually written"
    assert not result.ok and result.unknown


def test_last_action_limit_still_allows_final_verification(store, brief):
    job = store.create(brief)
    store.active()
    store.poll()
    store.update(job["id"], plan=sample_plan().model_dump())
    aid = store.enqueue(
        job["id"], Action(tool="file_read", goal_id="g1", args={"path": "report.txt"}).model_dump()
    )
    store.poll()
    store.acknowledge(aid, {"ok": True, "data": {"text": "Verified result"}})
    finish = Decision(
        decision=Finish(
            kind="finish",
            outcome="completed",
            summary="Report verified",
            evidence=[{"goal_id": "g1", "action_id": aid, "detail": "Actual readback"}],
            remaining=[],
        )
    )
    models = FixtureModels(
        [finish, Verdict(verified=True, reason="Verified content", satisfied_goals=["g1"])]
    )
    asyncio.run(Engine(store, models, max_steps=1).tick())
    assert store.job(job["id"])["status"] == "completed"


def test_action_history_does_not_drop_old_unknown_receipts(store, brief):
    job = store.create(brief)
    with store.tx() as db:
        for i in range(2001):
            db.execute(
                "INSERT INTO actions VALUES(?,?,?,'done',?,?)",
                (
                    str(i),
                    job["id"],
                    encode(Action(tool="observe").model_dump()),
                    encode({"ok": True, "data": {}}),
                    i,
                ),
            )
    assert len(store.actions(job["id"])) == 2001


def test_duplicate_acknowledgement_ignores_json_key_order(store, brief):
    job = store.create(brief)
    store.active()
    aid = store.enqueue(job["id"], Action(tool="observe").model_dump())
    store.poll()
    store.acknowledge(aid, {"ok": True, "data": {"first": 1, "second": 2}})
    store.acknowledge(aid, {"data": {"second": 2, "first": 1}, "ok": True})


def test_cancelled_report_tracks_late_billing_settlement(store, brief):
    job = store.create(brief)
    call = store.reserve(job["id"], 1)
    store.finish(job["id"], "cancelled", {"summary": "Cancelled", "spent_usd": 0})
    store.settle(call, 0.25)
    assert store.job(job["id"])["report"]["spent_usd"] == 0.25


@pytest.mark.parametrize("amount", [-1, float("inf"), float("nan")])
def test_budget_reservations_reject_invalid_values(store, brief, amount):
    job = store.create(brief)
    with pytest.raises(ValueError):
        store.reserve(job["id"], amount)


def test_old_screenshot_is_not_reused_as_current_visual_context():
    actions = [
        {
            "id": "old",
            "body": {"tool": "browser_observe", "args": {}},
            "result": {"ok": True, "data": {"screen": {"jpeg": "old-frame"}}},
        },
        {
            "id": "new",
            "body": {"tool": "file_list", "args": {}},
            "result": {"ok": True, "data": {"files": []}},
        },
    ]
    assert compact(actions)[1] is None


def test_cloud_failure_can_fall_back_to_text_only_local_planning(store, brief):
    job = store.create(brief)
    models = FixtureModels([ModelUnavailable("cloud budget exhausted"), sample_plan()])
    result = asyncio.run(
        Engine(store, models).ask(
            job, "Plan", {"latest_screen": "image"}, type(sample_plan()), cloud_first=True
        )
    )
    assert result.goals
    assert [c[1] for c in models.calls] == [True, False]
    assert "latest_screen" not in models.calls[-1][2]


def test_unknown_write_blocks_differently_spelled_followup_mutations(store, brief):
    job = store.create(brief)
    store.active()
    store.update(job["id"], plan=sample_plan().model_dump())
    aid = store.enqueue(
        job["id"],
        Action(
            tool="file_write",
            goal_id="g1",
            intent="local_change",
            authority_quote=brief.prompt,
            args={"path": "a.txt", "text": "first"},
        ).model_dump(),
    )
    store.poll()
    store.acknowledge(aid, {"ok": False, "unknown": True, "data": {}})
    next_action = Decision(
        decision=Continue(
            kind="action",
            note="Try another path",
            action=Action(
                tool="file_write",
                goal_id="g1",
                intent="local_change",
                authority_quote=brief.prompt,
                args={"path": "./a.txt", "text": "second"},
            ),
        )
    )
    engine = Engine(store, FixtureModels([next_action]))
    asyncio.run(engine.tick())
    observation = store.poll()
    store.acknowledge(observation["id"], {"ok": True, "data": {}})
    asyncio.run(engine.tick())
    assert len(store.actions(job["id"])) == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows job assignment")
def test_windows_program_never_runs_before_job_assignment(settings, tmp_path, monkeypatch):
    import win32job

    executor = Executor(settings, tmp_path / "workspace")
    settings.process_commands = [sys.executable]
    marker = executor.root / "escaped.txt"

    def reject_assignment(*args):
        time.sleep(0.3)
        raise RuntimeError("Injected job assignment failure")

    monkeypatch.setattr(win32job, "AssignProcessToJobObject", reject_assignment)
    with pytest.raises(RuntimeError, match="Injected job assignment failure"):
        executor._process(
            {
                "argv": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('escaped.txt').write_text('ran')",
                ],
                "cwd": ".",
                "timeout": 5,
            }
        )
    assert not marker.exists()

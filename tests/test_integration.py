import asyncio
import contextlib
import sys
import time

import httpx
import pytest

from autopilot.api import create_app
from autopilot.engine import Engine
from autopilot.schema import Action, Continue, Decision, Finish, Goal, Plan, Verdict
from autopilot.worker import Journal, run_worker


def test_missing_journal_on_redelivery_is_fenced(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite")
    assert journal.prepare("already-dispatched", redelivery=True)["unknown"]
    assert journal.prepare("already-dispatched")["unknown"]


def test_worker_requires_explicit_isolation(settings):
    with pytest.raises(ValueError, match="dedicated"):
        asyncio.run(run_worker(settings))


def test_real_queue_worker_files_and_python_process(settings, brief, monkeypatch):
    settings.isolated_worker = True
    settings.process_commands = [sys.executable]
    gateway = create_app(settings, worker_only=True)
    store = gateway.state.store
    job = store.create(brief)

    class Driver:
        async def generate(self, job_id, system, packet, schema, cloud=False):
            if schema is Plan:
                return Plan(
                    goals=[
                        Goal(
                            id="g1",
                            description="Create and test the local Python program",
                            check="Readback and successful Python exit with PASS",
                        )
                    ],
                    approach="Write, read and run a small synthetic program",
                )
            actions = store.actions(job_id)
            if schema is Verdict:
                process = [a for a in actions if a["body"]["tool"] == "process"][-1]
                assert process["result"]["data"]["exit_code"] == 0
                assert "PASS" in process["result"]["data"]["output"]
                return Verdict(
                    verified=True, reason="Real process and readback matched", satisfied_goals=["g1"]
                )
            if not actions:
                action = Action(
                    tool="file_write",
                    args={"path": "program.py", "text": "assert 2+2==4\nprint('PASS')\n"},
                    intent="local_change",
                    authority_quote=brief.prompt,
                )
            elif len(actions) == 1:
                action = Action(tool="file_read", args={"path": "program.py"})
            elif len(actions) == 2:
                action = Action(
                    tool="process",
                    args={"argv": [sys.executable, "program.py"], "timeout": 10},
                    intent="local_change",
                    authority_quote=brief.prompt,
                )
            else:
                return Decision(
                    decision=Finish(
                        kind="finish",
                        outcome="completed",
                        summary="Synthetic code verified",
                        evidence=[
                            {
                                "goal_id": "g1",
                                "action_id": a["id"],
                                "detail": "Actual readback/process result",
                            }
                            for a in actions
                            if a["body"]["tool"] in {"file_read", "process"}
                        ],
                        remaining=[],
                    )
                )
            action.goal_id = "g1"
            return Decision(
                decision=Continue(kind="action", note="Synthetic integration driver", action=action)
            )

    real_client = httpx.AsyncClient

    def client(**kwargs):
        kwargs.pop("verify", None)
        return real_client(transport=httpx.ASGITransport(app=gateway), **kwargs)

    monkeypatch.setattr("autopilot.worker.httpx.AsyncClient", client)

    async def scenario():
        engine = Engine(store, Driver())
        worker = asyncio.create_task(run_worker(settings))
        scheduler = asyncio.create_task(engine.run(0.02))
        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline and store.job(job["id"])["status"] != "completed":
                await asyncio.sleep(0.1)
            result = store.job(job["id"])
            assert result["status"] == "completed", result
            assert result["report"]["artifacts"]
            assert (settings.data_dir / "workspaces" / job["id"] / "program.py").exists()
        finally:
            scheduler.cancel()
            worker.cancel()
            for task in (scheduler, worker):
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    asyncio.run(scenario())

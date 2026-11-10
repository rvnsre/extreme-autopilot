import asyncio
import time

from autopilot.engine import Engine
from autopilot.models import ModelUnavailable
from autopilot.schema import Action, Decision, Finish, Goal, Plan, Verdict


class FixtureModels:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def generate(self, job_id, system, packet, schema, cloud=False):
        self.calls.append((schema, cloud, packet))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def sample_plan():
    return Plan(
        goals=[Goal(id="g1", description="Create a correct report", check="Read back the exact report")],
        approach="Write a local report then verify its content",
    )


def tick(engine):
    asyncio.run(engine.tick())


def test_real_engine_completion_needs_readback_and_independent_verifier(store, brief):
    job = store.create(brief)
    store.poll()
    store.update(job["id"], plan=sample_plan().model_dump())
    action = store.enqueue(job["id"], Action(tool="file_read", args={"path": "report.txt"}).model_dump())
    store.active()
    store.poll()
    store.acknowledge(action, {"ok": True, "data": {"text": "Verified result"}})
    with store.tx() as db:
        db.execute("UPDATE worker SET seen=?", (time.time() - 60,))
    finish = Decision(
        decision=Finish(
            kind="finish",
            outcome="completed",
            summary="Verified report",
            evidence=[{"goal_id": "g1", "action_id": action, "detail": "Readback matches"}],
            remaining=[],
        )
    )
    model = FixtureModels(
        [finish, Verdict(verified=True, reason="Readback proves the goal", satisfied_goals=["g1"])]
    )
    engine = Engine(store, model)
    tick(engine)
    assert store.job(job["id"])["status"] == "completed"
    assert model.calls[-1][1] is True


def test_fabricated_completion_cannot_pass(store, brief):
    job = store.create(brief)
    store.poll()
    store.update(job["id"], plan=sample_plan().model_dump())
    finish = Decision(
        decision=Finish(
            kind="finish",
            outcome="completed",
            summary="Everything done",
            evidence=[{"goal_id": "g1", "action_id": "invented", "detail": "Trust me"}],
            remaining=[],
        )
    )
    engine = Engine(store, FixtureModels([finish] * 3))
    for _ in range(3):
        tick(engine)
    assert store.job(job["id"])["status"] == "blocked"


def test_local_failure_escalates_to_cloud(store, brief):
    job = store.create(brief)
    store.poll()
    model = FixtureModels([ModelUnavailable("local down"), sample_plan()])
    tick(Engine(store, model))
    assert store.job(job["id"])["plan"]["goals"][0]["id"] == "g1"
    assert [call[1] for call in model.calls] == [False, True]


def test_restart_observes_before_planning_or_mutating(store, brief):
    store.create(brief)
    store.active()
    store.poll()
    store.recover()
    model = FixtureModels([])
    tick(Engine(store, model))
    assert store.poll()["action"]["tool"] == "observe"
    assert model.calls == []


def test_deadline_finishes_even_without_worker(store, brief):
    job = store.create(brief)
    with store.tx() as db:
        db.execute("UPDATE jobs SET deadline=? WHERE id=?", (time.time() - 1, job["id"]))
    tick(Engine(store, FixtureModels([])))
    assert store.job(job["id"])["status"] == "blocked"


def test_earlier_goal_evidence_survives_long_recent_history():
    from autopilot.engine import compact, evidence_index

    actions = [
        {
            "id": str(i),
            "body": {
                "tool": "file_read",
                "goal_id": "g1" if i == 0 else "g2",
                "args": {"path": "report.txt"},
            },
            "result": {"ok": True, "data": {"text": "verified"}},
        }
        for i in range(100)
    ]
    assert "0" not in {a["id"] for a in compact(actions)[0]}
    assert "0" in {e["action_id"] for e in evidence_index(actions)}
    assert len(evidence_index(actions)) == 3
    assert len(compact(actions, limit=30)[0]) == 30

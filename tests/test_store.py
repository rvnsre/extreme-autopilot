import concurrent.futures

import pytest
from pydantic import ValidationError

from autopilot.schema import Action, JobInput


@pytest.mark.parametrize("length", [0, 1, 199])
def test_short_prompt_rejected(length):
    with pytest.raises(ValidationError):
        JobInput(prompt="ə" * length)


@pytest.mark.parametrize("character", ["ə", "😀", "a"])
def test_unicode_boundary(character):
    assert len(JobInput(prompt="  " + character * 200 + "\n").prompt) == 200


def test_nonfinite_budget_rejected():
    with pytest.raises(ValidationError):
        JobInput(prompt="x" * 200, budget_usd=float("nan"))


def test_queue_is_exclusive(store, brief):
    first, second = store.create(brief), store.create(brief)
    assert store.active()["id"] == first["id"]
    assert store.active()["id"] == first["id"]
    store.finish(first["id"], "completed", {})
    assert store.active()["id"] == second["id"]


def test_poll_redelivers_same_id_without_duplicate_dispatch(store, brief):
    job = store.create(brief)
    store.active()
    action = store.enqueue(job["id"], Action(tool="observe").model_dump())
    assert store.poll()["id"] == store.poll()["id"] == action
    store.acknowledge(action, {"ok": True, "data": {}, "error": "", "unknown": False})
    assert store.poll() is None


def test_result_ack_is_idempotent_and_detects_conflict(store, brief):
    job = store.create(brief)
    store.active()
    action = store.enqueue(job["id"], Action(tool="observe").model_dump())
    store.poll()
    result = {"ok": True}
    store.acknowledge(action, result)
    store.acknowledge(action, result)
    with pytest.raises(ValueError):
        store.acknowledge(action, {"ok": False})


def test_result_must_follow_dispatch(store, brief):
    job = store.create(brief)
    action = store.enqueue(job["id"], Action(tool="observe").model_dump())
    with pytest.raises(ValueError):
        store.acknowledge(action, {"ok": True})


def test_only_one_outstanding_action(store, brief):
    job = store.create(brief)
    store.enqueue(job["id"], Action(tool="observe").model_dump())
    with pytest.raises(ValueError):
        store.enqueue(job["id"], Action(tool="observe").model_dump())


def test_budget_reservations_atomic(store, brief):
    job = store.create(brief)

    def reserve():
        try:
            return store.reserve(job["id"], 3)
        except ValueError:
            return None

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert sum(r is not None for r in results) == 1
    assert store.job(job["id"])["reserved"] == 3


def test_settlement_once_and_unknown_charge_after_restart(store, brief):
    job = store.create(brief)
    store.active()
    first = store.reserve(job["id"], 1)
    store.settle(first, 0.2)
    store.settle(first, 0.2)
    store.reserve(job["id"], 1)
    store.recover()
    store.recover()
    recovered = store.job(job["id"])
    assert recovered["spent"] == pytest.approx(1.2)
    assert recovered["reserved"] == 0
    assert recovered["status"] == "recovering"


def test_cancel_cannot_be_overwritten_by_late_finish_or_plan(store, brief):
    job = store.create(brief)
    store.finish(job["id"], "cancelled", {"summary": "cancel"})
    store.update(job["id"], status="running", plan={"goals": []})
    store.finish(job["id"], "completed", {"summary": "success"})
    assert store.job(job["id"])["status"] == "cancelled"
    assert store.job(job["id"])["plan"] is None
    assert store.poll() is None


def test_recent_errors_are_not_lost_after_first_event_page(store, brief):
    job = store.create(brief)
    with store.tx() as db:
        for i in range(210):
            store.event(db, job["id"], "progress", {"n": i})
        for i in range(5):
            store.event(db, job["id"], "error", {"message": str(i)})
    assert [e["message"] for e in store.recent_errors(job["id"])] == ["2", "3", "4"]

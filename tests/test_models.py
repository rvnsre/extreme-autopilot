import asyncio

import pytest
from test_engine import sample_plan

from autopilot.models import Models, ModelUnavailable
from autopilot.schema import Plan


class TransportModels(Models):
    response = None

    async def _request(self, url, body, headers=None):
        if url.endswith("/api/show"):
            return {"model_info": {"fixture.context_length": 32768}}
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_cloud_usage_billed_once(settings, store, brief):
    job = store.create(brief)
    model = TransportModels(settings, store)
    model.response = {
        "usage": {"prompt_tokens": 100, "completion_tokens": 100},
        "choices": [{"finish_reason": "stop", "message": {"content": sample_plan().model_dump_json()}}],
    }
    assert asyncio.run(model.generate(job["id"], "Return JSON", {}, Plan, cloud=True)).goals
    assert store.job(job["id"])["spent"] == pytest.approx(0.0005)
    assert store.job(job["id"])["reserved"] == 0


def test_timed_out_cloud_request_keeps_reserved_cost(settings, store, brief):
    job = store.create(brief)
    model = TransportModels(settings, store)
    model.response = TimeoutError()
    with pytest.raises(ModelUnavailable):
        asyncio.run(model.generate(job["id"], "Return JSON", {}, Plan, cloud=True))
    assert store.job(job["id"])["spent"] > 0
    assert store.job(job["id"])["reserved"] == 0


@pytest.mark.parametrize(
    "response",
    [
        {"done": False, "message": {"content": "{}"}},
        {"done": True, "done_reason": "length", "message": {"content": "{}"}},
        {"done": True, "done_reason": "stop", "message": {"content": "not JSON"}},
    ],
)
def test_incomplete_and_invalid_local_outputs_rejected(settings, store, brief, response):
    job = store.create(brief)
    model = TransportModels(settings, store)
    model.response = response
    with pytest.raises(ModelUnavailable):
        asyncio.run(model.generate(job["id"], "Return JSON", {}, Plan))


def test_local_model_names_are_not_mistaken_for_remote_aliases(settings, store, brief):
    settings.local_model = "cloudless-local-model"
    job = store.create(brief)
    model = TransportModels(settings, store)
    model.response = {
        "done": True,
        "done_reason": "stop",
        "message": {"content": sample_plan().model_dump_json()},
    }
    assert asyncio.run(model.generate(job["id"], "Return JSON", {}, Plan)).goals


def test_cancel_during_local_metadata_probe_prevents_generation(settings, store, brief):
    job = store.create(brief)

    class Cancelling(Models):
        async def _request(self, url, body, headers=None):
            assert url.endswith("/api/show")
            store.finish(job["id"], "cancelled", {"summary": "Cancelled during metadata"})
            return {"model_info": {"fixture.context_length": 32768}}

    with pytest.raises(ModelUnavailable, match="stopped"):
        asyncio.run(Cancelling(settings, store).generate(job["id"], "JSON", {}, Plan))

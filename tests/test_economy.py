"""Loss boundaries, cache isolation, billing, and real local extraction integrations."""

import asyncio
import base64
import io
import json
import random

import pytest
from fastapi.testclient import TestClient
from test_engine import sample_plan

pytest.importorskip("trafilatura", reason="Install the coordinator economy extra")

import msgspec
import zstandard

from autopilot.api import create_app
from autopilot.economy import Economy, Envelope, canonical, library_status
from autopilot.economy_benchmark import benchmark
from autopilot.models import ModelDeferred, Models, ModelUnavailable
from autopilot.schema import Decision, JobInput, Plan, Verdict


@pytest.fixture
def economy(tmp_path):
    optimizer = Economy(tmp_path / "economy", text_limit=800)
    yield optimizer
    optimizer.close()


def packet(text):
    return {
        "original_brief": "Azərbaycan dilində dəqiq hesabat yarat. Keep authority unchanged.",
        "recent_actions": [
            {
                "id": "receipt-1",
                "body": {"tool": "http_get", "authority_quote": "exact"},
                "result": {
                    "ok": True,
                    "unknown": False,
                    "data": {"text": text, "artifact": {"sha256": "original-hash"}},
                },
            }
        ],
    }


def test_all_22_libraries_have_real_installed_integrations():
    libraries = library_status()
    assert len(libraries) == 22 and len({x["name"] for x in libraries}) == 22
    assert all(x["version"] and x["role"] for x in libraries)


def test_compact_serialization_preserves_unicode_and_rejects_nonfinite():
    assert canonical({"b": "Ə", "a": 1}) == '{"a":1,"b":"Ə"}'
    with pytest.raises(ValueError):
        canonical({"value": float("nan")})


def test_large_receipt_integer_precision_is_preserved(economy):
    source = packet("Observation " * 1000)
    source["recent_actions"][0]["result"]["data"]["receipt_id"] = 10**80 + 7
    prepared, _ = economy.prepare(source)
    assert prepared["recent_actions"][0]["result"]["data"]["receipt_id"] == 10**80 + 7


def test_html_extraction_removes_noise_retains_sources(economy):
    text = (
        "<html><nav>SECRET MENU</nav><article><p>Reliable article content.</p></article>"
        '<script>BAD()</script><a href="/source">Source</a></html>'
    )
    extracted, links = economy.reducer.extract_html(text, "https://example.com/page")
    assert "BAD()" not in extracted and "SECRET MENU" not in extracted
    assert "Reliable article" in extracted and links[0]["url"] == "https://example.com/source"


def test_tables_and_code_survive_markdown_extraction(economy):
    text = (
        "<html><table><tr><th>Value</th></tr><tr><td>17</td></tr></table><pre>assert value == 17</pre></html>"
    )
    extracted, _ = economy.reducer.extract_html(text)
    assert "17" in extracted and "assert value == 17" in extracted and "|" in extracted


def test_exact_duplicates_removed_fuzzy_facts_retained(economy):
    paragraph = (
        "Measured task result: observation must preserve concrete values and all reference identifiers."
    )
    changed = paragraph.replace("result:", "result 2:")
    excerpt, meta = economy.reducer.reduce("\n\n".join([paragraph, paragraph, changed]), "", limit=2000)
    assert meta["exact_duplicates"] == 1
    assert changed in excerpt and paragraph in excerpt


def test_ranked_excerpt_preserves_authority_and_receipt_metadata(economy):
    source = packet("\n\n".join(f"Observation {i}: " + "dependency testing result " * 30 for i in range(30)))
    prepared, report = economy.prepare(source)
    assert prepared["original_brief"] == source["original_brief"]
    assert prepared["recent_actions"][0]["body"] == source["recent_actions"][0]["body"]
    data = prepared["recent_actions"][0]["result"]["data"]
    assert data["artifact"]["sha256"] == "original-hash" and data["excerpt"]["is_excerpt"]
    assert report["after_bytes"] < report["before_bytes"]
    assert "excerpt" not in source["recent_actions"][0]["result"]["data"]


def test_final_verification_never_gets_lossy_excerpt(economy):
    source = packet("A fact and its caveat.\n\n" * 1000)
    prepared, report = economy.prepare(source, final=True)
    assert prepared == source and report["excerpt_count"] == 0


def test_image_transport_is_never_counted_as_text_tokens(economy):
    source = {"original_brief": "Inspect the image.", "latest_screen": "A" * 250000}
    prepared, report = economy.prepare(source, final=True)
    assert prepared == source and report["image_present"]
    assert report["before_tokens"] < 200 and report["after_tokens"] < 200


def test_schema_title_fields_and_default_payloads_survive(economy):
    source = {
        "tools": {
            "example": {
                "title": "Example",
                "type": "object",
                "properties": {"title": {"type": "string", "title": "Title", "minLength": 3}},
                "default": {"title": "keep"},
                "required": ["title"],
            }
        }
    }
    prepared, _ = economy.prepare(source)
    schema = prepared["tools"]["example"]
    assert "title" not in schema and "title" in schema["properties"]
    assert schema["default"] == {"title": "keep"} and schema["properties"]["title"]["minLength"] == 3


def test_cache_is_job_model_schema_and_context_scoped(economy):
    key = economy.key("job1", "local:model", "system", {}, Plan)
    economy.put_plan(key, sample_plan())
    assert economy.get_plan(key, Plan) == sample_plan()
    for different in [
        economy.key("job2", "local:model", "system", {}, Plan),
        economy.key("job1", "cloud:model", "system", {}, Plan),
        economy.key("job1", "local:model", "system", {"new": True}, Plan),
    ]:
        assert economy.get_plan(different, Plan) is None


def test_planning_needs_tool_names_but_decisions_keep_argument_schemas(economy):
    from autopilot.executor import tool_catalog

    source = {"tools": tool_catalog(), "original_brief": "Keep this unchanged"}
    planned, stats = economy.prepare(source, planning=True)
    decision, _ = economy.prepare(source)
    assert planned["tools"] == sorted(tool_catalog())
    assert isinstance(decision["tools"]["file_read"], dict)
    assert stats["after_bytes"] < stats["before_bytes"]


@pytest.mark.parametrize("kind", ["digest", "schema", "bomb", "expired"])
def test_corrupt_invalid_large_and_expired_cache_never_reused(economy, kind):
    key = "test"
    economy.put_plan(key, sample_plan())
    if kind == "digest":
        envelope = msgspec.msgpack.decode(economy.cache.get("plan:" + key), type=Envelope)
        envelope.digest = "wrong"
        economy.cache.set("plan:" + key, msgspec.msgpack.encode(envelope))
    elif kind == "schema":
        from blake3 import blake3

        payload = b'{"goals":[],"approach":"invalid"}'
        envelope = Envelope(1, blake3(payload).hexdigest(), zstandard.ZstdCompressor().compress(payload))
        economy.cache.set("plan:" + key, msgspec.msgpack.encode(envelope))
    elif kind == "bomb":
        envelope = Envelope(1, "x", zstandard.ZstdCompressor().compress(b"A" * 128001))
        economy.cache.set("plan:" + key, msgspec.msgpack.encode(envelope))
    else:
        economy.cache.set("plan:" + key, b"expired", expire=-1)
    assert economy.get_plan(key, Plan) is None


def test_offline_token_fallback_does_not_fetch(economy, monkeypatch):
    monkeypatch.setattr(
        "tiktoken.get_encoding", lambda *args: pytest.fail("Unexpected network-capable tokenizer load")
    )
    assert economy.meter.count("Ə") == {"tokens": 2, "basis": "utf8_upper_bound"}


def test_projection_and_storage_compression_are_explicit(economy):
    result = economy.analyze({"data": {"body": "Relevant information. " * 100}}, "data.body")
    assert result["compression_is_not_token_savings"]
    assert result["compressed_storage_bytes"] < result["wire_bytes"]
    with pytest.raises(ValueError):
        economy.analyze({}, "x" * 501)


def test_bad_tokenizer_assets_fall_back_without_network(economy):
    (economy.root / "tokenizer.json").write_text("broken", encoding="utf-8")
    assert economy.meter.count("hello")["basis"] == "utf8_upper_bound"


def test_fenced_code_indentation_is_not_normalized(economy):
    text = "```python\ndef f():\n    return 17\n```"
    excerpt, _ = economy.reducer.reduce(text, "", 2000)
    assert "    return 17" in excerpt


def test_synthetic_benchmark_preserves_final_evidence(economy):
    result = benchmark(economy)
    assert result["synthetic_only"] and not result["live_quality_verified"]
    assert all(row["no_model_request"] and row["final_evidence_unchanged"] for row in result["cases"])
    assert result["provider_dollars_saved"] is None


class CountedModels(Models):
    calls = 0
    fail = False
    bodies = None
    payload = None

    async def _request(self, url, body, headers=None):
        if url.endswith("/api/show"):
            return {"model_info": {"fixture.context_length": 32768}}
        self.calls += 1
        self.bodies = body
        await asyncio.sleep(0.01)
        if self.fail:
            raise TimeoutError("synthetic timeout")
        return {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 40},
            },
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": self.payload or sample_plan().model_dump_json()},
                }
            ],
        }


@pytest.mark.parametrize("enabled", [False, True])
def test_large_real_jpeg_has_separate_text_and_transport_limits(settings, store, brief, enabled):
    from PIL import Image

    settings.economy_enabled = enabled
    stream = io.BytesIO()
    Image.frombytes("RGB", (640, 480), random.Random(1).randbytes(640 * 480 * 3)).save(
        stream, "JPEG", quality=80
    )
    raw = stream.getvalue()
    assert 135000 < len(raw) <= 256000
    jpeg = base64.b64encode(raw).decode()
    job = store.create(brief)
    model = CountedModels(settings, store)
    try:
        assert asyncio.run(model.generate(job["id"], "JSON", {"latest_screen": jpeg}, Plan, True)).goals
        content = model.bodies["messages"][1]["content"]
        assert jpeg not in content[0]["text"]
        assert content[1]["image_url"]["url"].endswith(jpeg)
    finally:
        model.close()


@pytest.mark.parametrize("image", ["invalid-base64", base64.b64encode(b"not jpeg").decode(), "A" * 344001])
def test_bad_images_rejected_before_paid_request(settings, store, brief, image):
    settings.economy_enabled = True
    job = store.create(brief)
    model = CountedModels(settings, store)
    try:
        with pytest.raises(ModelUnavailable, match="JPEG"):
            asyncio.run(model.generate(job["id"], "JSON", {"latest_screen": image}, Plan, True))
        assert model.calls == 0 and store.job(job["id"])["spent"] == 0
    finally:
        model.close()


def test_concurrent_plan_requests_coalesce_and_bill_once(settings, store, brief):
    settings.economy_enabled = True
    job = store.create(brief)
    model = CountedModels(settings, store)
    try:

        async def run():
            return await asyncio.gather(
                *(model.generate(job["id"], "JSON", {}, Plan, True) for _ in range(3))
            )

        assert len(asyncio.run(run())) == 3
        assert model.calls == 1 and store.job(job["id"])["spent"] == pytest.approx(0.0003)
        assert model.bodies["max_completion_tokens"] == 1024
        summary = model.economy.summary(job["id"])["totals"]
        assert summary["cache_hit"] == 2 and summary["provider_cached_tokens"] == 40
        assert not summary["actual_provider_savings_known"]
    finally:
        model.close()


def test_cancellation_prevents_cached_plan_reuse(settings, store, brief):
    settings.economy_enabled = True
    job = store.create(brief)
    model = CountedModels(settings, store)
    try:
        asyncio.run(model.generate(job["id"], "JSON", {}, Plan, True))
        store.finish(job["id"], "cancelled", {"summary": "cancelled"})
        with pytest.raises(ModelUnavailable, match="stopped"):
            asyncio.run(model.generate(job["id"], "JSON", {}, Plan, True))
        assert model.calls == 1
    finally:
        model.close()


@pytest.mark.parametrize("enabled,expected", [(False, 2048), (True, 1024)])
def test_cloud_output_ceiling_does_not_reduce_local_capacity(settings, store, brief, enabled, expected):
    settings.economy_enabled = enabled
    settings.cloud_output_tokens = 256
    job = store.create(brief)

    class LocalCapture(Models):
        async def _request(self, url, body, headers=None):
            if url.endswith("/api/show"):
                return {"model_info": {"fixture.context_length": 32768}}
            assert body["options"]["num_predict"] == expected
            return {
                "done": True,
                "done_reason": "stop",
                "message": {"content": sample_plan().model_dump_json()},
            }

    model = LocalCapture(settings, store)
    try:
        assert asyncio.run(model.generate(job["id"], "JSON", {}, Plan)).goals
    finally:
        model.close()


def test_cloud_cooldown_makes_no_second_paid_request(settings, store, brief):
    settings.economy_enabled = True
    job = store.create(brief)
    model = CountedModels(settings, store)
    model.fail = True
    try:
        with pytest.raises(ModelUnavailable):
            asyncio.run(model.generate(job["id"], "JSON", {}, Plan, True))
        spent = store.job(job["id"])["spent"]
        with pytest.raises(ModelDeferred):
            asyncio.run(model.generate(job["id"], "JSON", {}, Plan, True))
        assert model.calls == 1 and store.job(job["id"])["spent"] == spent
    finally:
        model.close()


def test_cooldown_defers_engine_without_false_failure(store, brief):
    from autopilot.engine import Engine

    class DeferredModels:
        async def generate(self, *args):
            raise ModelDeferred("Cooling down")

    job = store.create(brief)
    store.active()
    store.update(job["id"], plan=sample_plan().model_dump())
    asyncio.run(Engine(store, DeferredModels()).tick())
    assert store.job(job["id"])["status"] == "running"
    assert store.job(job["id"])["errors"] == 0


@pytest.mark.parametrize("schema", [Decision, Verdict])
def test_invalid_outputs_and_final_verdicts_never_cached(settings, store, brief, schema):
    settings.economy_enabled = True
    job = store.create(brief)
    model = CountedModels(settings, store)
    try:
        for _ in range(2):
            with pytest.raises(ModelUnavailable):
                asyncio.run(model.generate(job["id"], "JSON", {}, schema, True))
            model.economy.failures.clear()
        assert model.calls == 2
        assert not any(str(key).startswith("plan:") for key in model.economy.cache)
    finally:
        model.close()


@pytest.mark.parametrize(
    "schema,payload",
    [
        (
            Decision,
            {
                "decision": {
                    "kind": "action",
                    "note": "Observe first",
                    "action": {"tool": "observe", "goal_id": "g1"},
                }
            },
        ),
        (Verdict, {"verified": True, "reason": "Evidence inspected", "satisfied_goals": ["g1"]}),
    ],
)
def test_valid_decisions_and_verdicts_are_regenerated(settings, store, brief, schema, payload):
    settings.economy_enabled = True
    job = store.create(brief)
    model = CountedModels(settings, store)
    model.payload = json.dumps(payload)
    try:
        for _ in range(2):
            assert asyncio.run(model.generate(job["id"], "JSON", {}, schema, True))
        assert model.calls == 2
        assert not any(str(key).startswith("plan:") for key in model.economy.cache)
    finally:
        model.close()


def test_economy_api_is_authenticated_and_metrics_do_not_contain_briefs(settings):
    settings.economy_enabled = True
    app = create_app(settings, run_engine=False)
    with TestClient(app) as client:
        assert client.get("/api/economy").status_code == 401
        headers = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
        assert len(client.get("/api/economy", headers=headers).json()["libraries"]) == 22
        job = app.state.store.create(JobInput(prompt="private task " * 30))
        economy = app.state.engine.models.economy
        _, report = economy.prepare(packet("private evidence " * 500))
        economy.record(job["id"], report)
        response = client.get(f"/api/jobs/{job['id']}/economy", headers=headers)
        assert response.status_code == 200
        assert "private evidence" not in response.text and "private task" not in response.text

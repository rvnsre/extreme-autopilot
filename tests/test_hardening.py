import asyncio
import contextlib
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from filelock import FileLock
from pydantic import ValidationError
from test_engine import FixtureModels, sample_plan

from autopilot.api import create_app
from autopilot.engine import Engine
from autopilot.models import ModelUnavailable
from autopilot.schema import Action, ToolResult
from autopilot.store import Store
from autopilot.worker import run_worker


def test_single_worker_lease_fences_concurrent_sessions_and_late_results(store, brief):
    job = store.create(brief)
    store.active()
    action = store.enqueue(job["id"], Action(tool="observe").model_dump())
    assert not store.poll("first")["redelivery"]
    with pytest.raises(ValueError, match="lease"):
        store.poll("second")
    with store.tx() as db:
        db.execute("UPDATE worker SET seen=?", (time.time() - 46,))
    assert store.poll("second")["redelivery"]
    with pytest.raises(ValueError, match="lease"):
        store.acknowledge(action, {"ok": True}, session="first")
    store.acknowledge(action, {"ok": False, "unknown": True}, session="second")
    assert store.job(job["id"])["status"] == "recovering"


def test_idempotent_submission_is_atomic_and_rejects_changed_payload(store, brief):
    key = "same-request-key-123"
    with ThreadPoolExecutor(2) as pool:
        ids = list(pool.map(lambda _: store.create(brief, key)["id"], range(2)))
    assert len(set(ids)) == 1 and len(store.jobs()) == 1
    with pytest.raises(ValueError, match="different brief"):
        store.create(brief.model_copy(update={"budget_usd": 4}), key)


def test_api_idempotency_and_private_cache_headers(settings, brief):
    app = create_app(settings, run_engine=False)
    with TestClient(app, headers={"Authorization": "Bearer " + "a" * 48}) as client:
        first = client.post("/api/jobs", json=brief.model_dump(), headers={"Idempotency-Key": "a" * 32})
        second = client.post("/api/jobs", json=brief.model_dump(), headers={"Idempotency-Key": "a" * 32})
        assert first.status_code == second.status_code == 201
        assert first.json()["id"] == second.json()["id"]
        assert first.headers["Cache-Control"] == "no-store"
        assert first.headers["X-Content-Type-Options"] == "nosniff"
        assert (
            client.post(
                "/api/jobs", json=brief.model_dump(), headers={"Idempotency-Key": "short"}
            ).status_code
            == 409
        )


def test_worker_protocol_requires_session_header(settings):
    with TestClient(
        create_app(settings, worker_only=True), headers={"Authorization": "Bearer " + "w" * 48}
    ) as client:
        assert client.post("/worker/poll").status_code == 422
        assert client.post("/worker/poll", headers={"X-Worker-Session": "a" * 32}).status_code == 200
        assert client.post("/worker/poll", headers={"X-Worker-Session": "b" * 32}).status_code == 409


def test_migration_preserves_v01_database_and_is_safe_under_concurrent_open(tmp_path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE jobs(id TEXT PRIMARY KEY,prompt TEXT,status TEXT,created REAL,
        deadline REAL,budget REAL,spent REAL DEFAULT 0,reserved REAL DEFAULT 0,plan TEXT,report TEXT,
        errors INTEGER DEFAULT 0,
        step INTEGER DEFAULT 0,uncertain INTEGER DEFAULT 0);
        CREATE TABLE worker(id INTEGER PRIMARY KEY,seen REAL);
        INSERT INTO jobs(id,prompt,status,created,deadline,budget)
        VALUES('old','preserve','queued',0,9999999999,5);
    """)
    db.close()
    with ThreadPoolExecutor(3) as pool:
        stores = list(pool.map(lambda _: Store(path), range(3)))
    assert stores[0].job("old")["prompt"] == "preserve"
    assert "request_key" in stores[0].job("old")


def test_recovery_refreshes_cancelled_billing_report(store, brief):
    job = store.create(brief)
    store.reserve(job["id"], 2)
    store.finish(job["id"], "cancelled", {"summary": "Cancelled"})
    store.recover()
    report = store.job(job["id"])["report"]
    assert report["spent_usd"] == 2 and report["reserved_usd"] == 0


def test_step_limit_does_not_cancel_a_still_pending_operation(store, brief):
    job = store.create(brief)
    store.active()
    store.poll()
    store.enqueue(job["id"], Action(tool="observe").model_dump())
    asyncio.run(Engine(store, FixtureModels([]), max_steps=1).tick())
    assert store.job(job["id"])["status"] == "running"


def test_deadline_is_checked_between_model_providers(store, brief):
    job = store.create(brief)

    class ExpiringModel:
        calls = 0

        async def generate(self, *args, **kwargs):
            self.calls += 1
            with store.tx() as db:
                db.execute("UPDATE jobs SET deadline=? WHERE id=?", (time.time() - 1, job["id"]))
            raise ModelUnavailable("local request expired")

    model = ExpiringModel()
    with pytest.raises(ModelUnavailable, match="deadline"):
        asyncio.run(Engine(store, model).ask(job, "plan", {}, type(sample_plan())))
    assert model.calls == 1
    with pytest.raises(ValueError):
        store.reserve(job["id"], 0.01)


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": "true"},
        {"ok": True, "unknown": True},
        {"ok": True, "data": {"artifact": {"path": "x", "content_b64": "eA==", "sha256": "wrong"}}},
    ],
)
def test_untrusted_receipts_must_be_strict_and_integrity_checked(payload):
    with pytest.raises(ValidationError):
        ToolResult.model_validate(payload)


def test_worker_setup_failure_releases_its_lock(settings, tmp_path):
    settings.isolated_worker = True
    settings.ca_file = str(tmp_path / "missing-ca.pem")
    with pytest.raises(FileNotFoundError) as error:
        asyncio.run(run_worker(settings))
    assert error.value  # Keep the traceback alive while checking resource release.
    with FileLock(settings.data_dir / "worker.lock", timeout=0):
        pass


def test_worker_rechecks_cancellation_before_any_input(settings, brief, monkeypatch):
    settings.isolated_worker = True
    app = create_app(settings, worker_only=True)
    store = app.state.store
    job = store.create(brief)
    store.active()
    action = store.enqueue(
        job["id"],
        Action(
            tool="file_write",
            intent="local_change",
            authority_quote=brief.prompt,
            args={"path": "must-not-exist.txt", "text": "no"},
        ).model_dump(),
    )
    real_client = httpx.AsyncClient

    class CancelTransport(httpx.ASGITransport):
        async def handle_async_request(self, request):
            if request.url.path == "/worker/heartbeat":
                store.finish(job["id"], "cancelled", {"summary": "Cancelled before input"})
            return await super().handle_async_request(request)

    def client(**kwargs):
        kwargs.pop("verify", None)
        return real_client(transport=CancelTransport(app=app), **kwargs)

    monkeypatch.setattr("autopilot.worker.httpx.AsyncClient", client)

    async def scenario():
        worker = asyncio.create_task(run_worker(settings))
        try:
            for _ in range(50):
                rows = store.actions(job["id"])
                if rows[0]["status"] == "done":
                    break
                await asyncio.sleep(0.05)
            result = store.actions(job["id"])[0]
            assert result["id"] == action and result["status"] == "done"
            assert not result["result"]["ok"] and not result["result"]["unknown"]
            assert not (settings.data_dir / "workspaces" / job["id"] / "must-not-exist.txt").exists()
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    asyncio.run(scenario())


def test_upgrade_cannot_migrate_a_running_legacy_coordinator(tmp_path):
    from filelock import Timeout

    path = tmp_path / "legacy.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE worker(id INTEGER PRIMARY KEY,seen REAL)")
    db.close()
    with FileLock(tmp_path / "coordinator.lock", timeout=0):
        with pytest.raises(Timeout):
            Store(path)
    db = sqlite3.connect(path)
    try:
        assert {r[1] for r in db.execute("PRAGMA table_info(worker)")} == {"id", "seen"}
    finally:
        db.close()


def test_dead_scheduler_is_not_reported_as_healthy(settings):
    class Stopped:
        def done(self):
            return True

    app = create_app(settings, run_engine=False)
    app.state.scheduler = Stopped()
    with TestClient(app, headers={"Authorization": "Bearer " + "a" * 48}) as client:
        response = client.get("/api/health")
        assert response.status_code == 503 and not response.json()["scheduler_running"]


@pytest.mark.parametrize(
    "url", ["https://localhost/path", "https://localhost?token=secret", "https://localhost:bad"]
)
def test_bad_worker_urls_fail_before_any_network_request(url):
    from autopilot.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, server_url=url)


def test_settings_validation_does_not_print_secret_inputs():
    from autopilot.config import Settings

    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, openai_key="PRIVATE-SECRET-VALUE", server_url="http://bad")
    assert "PRIVATE-SECRET-VALUE" not in str(error.value)


def test_credential_profile_cannot_include_paths_or_header_injection():
    from autopilot.config import ApiCredential

    with pytest.raises(ValidationError):
        ApiCredential(origin="https://api.github.com/path", token="safe")
    with pytest.raises(ValidationError):
        ApiCredential(origin="https://api.github.com", token="bad\r\nheader")


def test_ambiguous_authentication_headers_are_rejected(settings):
    with TestClient(create_app(settings, run_engine=False)) as client:
        response = client.get(
            "/api/health",
            headers=[("Authorization", "Bearer " + "a" * 48), ("Authorization", "Bearer " + "a" * 48)],
        )
        assert response.status_code == 400


def test_duplicate_acknowledgements_preserve_json_value_types(store, brief):
    job = store.create(brief)
    store.active()
    action = store.enqueue(job["id"], Action(tool="observe").model_dump())
    store.poll()
    store.acknowledge(action, {"ok": True, "data": {"receipt": True}})
    with pytest.raises(ValueError, match="Conflicting"):
        store.acknowledge(action, {"ok": True, "data": {"receipt": 1}})

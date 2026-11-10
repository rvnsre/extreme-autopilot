import base64
import hashlib
import time

from fastapi.testclient import TestClient

from autopilot.api import create_app
from autopilot.schema import Action


def test_auth_origin_and_role_separation(settings, brief):
    app = create_app(settings, run_engine=False)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 401
        worker = {"Authorization": "Bearer " + "w" * 48}
        admin = {"Authorization": "Bearer " + "a" * 48}
        assert client.get("/api/health", headers=worker).status_code == 401
        assert client.post("/worker/poll", headers=worker).status_code == 404
        assert (
            client.get("/api/health", headers={**admin, "Origin": "https://evil.example"}).status_code == 403
        )
        assert client.get("/api/health", headers=admin).status_code == 200


def test_job_validation_cancel_and_report(settings, brief):
    with TestClient(
        create_app(settings, run_engine=False), headers={"Authorization": "Bearer " + "a" * 48}
    ) as c:
        assert c.post("/api/jobs", json={"prompt": "x" * 199}).status_code == 422
        r = c.post("/api/jobs", json=brief.model_dump())
        assert r.status_code == 201
        job_id = r.json()["id"]
        assert c.get(f"/api/jobs/{job_id}/report").status_code == 409
        assert c.post(f"/api/jobs/{job_id}/cancel").json()["status"] == "cancelled"
        assert c.get(f"/api/jobs/{job_id}/report").json()["status"] == "cancelled"
        assert c.get("/api/jobs/unknown").status_code == 404
        assert c.get(f"/api/jobs/{job_id}/events?after=-1").status_code == 422


def test_worker_protocol_and_artifact_download(settings, brief):
    app = create_app(settings, run_engine=False)
    gateway = create_app(settings, worker_only=True)
    store = app.state.store
    job = store.create(brief)
    store.active()
    action = store.enqueue(job["id"], Action(tool="file_read", args={"path": "report.txt"}).model_dump())
    with TestClient(
        gateway, headers={"Authorization": "Bearer " + "w" * 48, "X-Worker-Session": "1" * 32}
    ) as worker:
        command = worker.post("/worker/poll").json()["command"]
        assert command["id"] == action
        assert worker.get("/api/health").status_code == 404
        data = {
            "artifact": {
                "path": "report.txt",
                "sha256": hashlib.sha256(b"done").hexdigest(),
                "content_b64": base64.b64encode(b"done").decode(),
            }
        }
        body = {"action_id": action, "result": {"ok": True, "data": data}}
        assert worker.post("/worker/result", json=body).status_code == 200
        assert worker.post("/worker/result", json=body).status_code == 200
        assert worker.get("/worker/heartbeat", params={"job_id": job["id"]}).json()["deadline"] > time.time()
    with TestClient(app, headers={"Authorization": "Bearer " + "a" * 48}) as c:
        assert c.get(f"/api/jobs/{job['id']}/artifacts/{action}").content == b"done"
        assert c.get(f"/api/jobs/{job['id']}/screen").status_code == 404


def test_body_limit_after_auth(settings):
    with TestClient(create_app(settings, run_engine=False)) as c:
        assert c.post("/api/jobs", content=b"x" * 2_000_001).status_code == 401
        assert (
            c.post(
                "/api/jobs", content=b"x" * 2_000_001, headers={"Authorization": "Bearer " + "a" * 48}
            ).status_code
            == 413
        )

import asyncio
import base64
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from filelock import FileLock

from autopilot.config import Settings
from autopilot.engine import Engine
from autopilot.models import Models
from autopilot.schema import JobInput, WorkerResult
from autopilot.store import Store


class BoundaryMiddleware:
    def __init__(self, app, settings, worker_only=False):
        self.app, self.s, self.worker_only = app, settings, worker_only

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        protected = path.startswith(("/api/", "/worker/"))
        original_send = send

        async def secured_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-content-type-options", b"nosniff"))
                headers.append(
                    (
                        b"content-security-policy",
                        b"default-src 'self'; script-src 'self'; style-src 'self'; "
                        b"img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; "
                        b"base-uri 'none'; object-src 'none'",
                    )
                )
                if protected:
                    headers = [(k, v) for k, v in headers if k.lower() != b"cache-control"]
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await original_send(message)

        send = secured_send
        if not protected:
            if self.worker_only:
                return await JSONResponse({"detail": "Not found"}, 404)(scope, receive, send)
            return await self.app(scope, receive, send)
        if path.startswith("/worker/") != self.worker_only:
            return await JSONResponse({"detail": "Not found"}, 404)(scope, receive, send)
        for name in (b"authorization", b"origin", b"idempotency-key", b"x-worker-session"):
            if sum(k.lower() == name for k, _ in scope["headers"]) > 1:
                return await JSONResponse({"detail": "Ambiguous duplicate header"}, 400)(scope, receive, send)
        headers = {k.lower(): v for k, v in scope["headers"]}
        origin = headers.get(b"origin", b"").decode("latin1")
        if origin and (self.worker_only or origin not in self.s.ui_origins):
            return await JSONResponse({"detail": "Origin rejected"}, 403)(scope, receive, send)
        expected = self.s.worker_token if self.worker_only else self.s.api_token
        supplied = headers.get(b"authorization", b"")
        if not hmac.compare_digest(supplied, ("Bearer " + expected.get_secret_value()).encode()):
            return await JSONResponse({"detail": "Unauthorized"}, 401)(scope, receive, send)
        body = bytearray()
        try:
            async with asyncio.timeout(30):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > 2_000_000:
                        return await JSONResponse({"detail": "Body too large"}, 413)(scope, receive, send)
                    if not message.get("more_body"):
                        break
        except TimeoutError:
            return await JSONResponse({"detail": "Body transfer timed out"}, 408)(scope, receive, send)
        sent = False

        async def bounded_receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)


def create_app(settings=None, *, worker_only=False, run_engine=True, models=None):
    s = settings or Settings()
    s.require_tokens()
    store = Store(s.data_dir / "coordinator.sqlite")
    engine = Engine(store, models or Models(s, store), s.max_steps)

    @asynccontextmanager
    async def lifespan(app):
        task, lock = None, None
        try:
            if not worker_only and run_engine:
                lock = FileLock(s.data_dir / "coordinator.lock", timeout=0)
                lock.acquire()
                store.recover()
                task = asyncio.create_task(engine.run(s.tick_seconds))
                app.state.scheduler = task
            yield
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if lock:
                lock.release()
            close_models = getattr(engine.models, "close", None)
            if close_models:
                close_models()

    app = FastAPI(
        title="Extreme Autopilot",
        version="0.4.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.store, app.state.engine = store, engine
    app.state.scheduler = None
    app.add_middleware(BoundaryMiddleware, settings=s, worker_only=worker_only)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "Not found"}, 404)

    @app.exception_handler(ValueError)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)[:500]}, 409)

    if worker_only:

        @app.post("/worker/poll")
        async def poll(session: str = Header(..., alias="X-Worker-Session", pattern=r"^[0-9a-f]{32}$")):
            return {"protocol": 2, "command": store.poll(session)}

        @app.get("/worker/heartbeat")
        async def heartbeat(
            job_id: str = "", session: str = Header(..., alias="X-Worker-Session", pattern=r"^[0-9a-f]{32}$")
        ):
            return store.heartbeat(session, job_id)

        @app.post("/worker/result")
        async def result(
            payload: WorkerResult,
            session: str = Header(..., alias="X-Worker-Session", pattern=r"^[0-9a-f]{32}$"),
        ):
            store.acknowledge(payload.action_id, payload.result.model_dump(), session)
            return {"accepted": True}

        return app

    @app.get("/api/health")
    async def health():
        task = app.state.scheduler
        healthy = task is None or not task.done()
        return JSONResponse(
            {
                "service": "extreme-autopilot",
                "worker_online": store.worker_online(),
                "scheduler_running": None if task is None else healthy,
            },
            200 if healthy else 503,
        )

    @app.get("/api/openapi.json")
    async def openapi():
        return app.openapi()

    @app.post("/api/jobs", status_code=201)
    async def create(
        payload: JobInput, request_key: str | None = Header(default=None, alias="Idempotency-Key")
    ):
        return store.create(payload, request_key)

    @app.get("/api/jobs")
    async def jobs():
        return store.jobs()

    @app.get("/api/economy")
    async def economy_status():
        economy = getattr(engine.models, "economy", None)
        if economy is None:
            return {"enabled": False, "install": "Install requirements-economy.lock.txt on the coordinator"}
        from autopilot.economy import library_status

        return {
            "enabled": True,
            "libraries": library_status(),
            "plan_cache_ttl": economy.ttl,
            "context_text_limit": economy.text_limit,
            "token_basis": economy.meter.count("")["basis"],
            "automatic_provider_retries": False,
        }

    @app.get("/api/jobs/{job_id}/economy")
    async def economy_job(job_id: str):
        store.job(job_id)
        economy = getattr(engine.models, "economy", None)
        return economy.summary(job_id) if economy else {"enabled": False}

    @app.get("/api/jobs/{job_id}")
    async def job(job_id: str):
        return store.job(job_id)

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel(job_id: str):
        job = store.job(job_id)
        engine.finish(job, "cancelled", "İstifadəçi icranı dayandırdı.")
        return store.job(job_id)

    @app.get("/api/jobs/{job_id}/events")
    async def events(job_id: str, after: int = 0, summary: bool = False):
        store.job(job_id)
        if after < 0:
            raise HTTPException(422, "Invalid event cursor")
        rows = store.events(job_id, after)
        if summary:
            for row in rows:
                row["data"] = {
                    k: v for k, v in row["data"].items() if k in {"tool", "message", "error", "note"}
                }
        return rows

    @app.get("/api/jobs/{job_id}/stream")
    async def stream(job_id: str, request: Request, after: int = 0):
        store.job(job_id)

        async def generate():
            cursor = max(0, after)
            while not await request.is_disconnected():
                rows = store.events(job_id, cursor)
                for row in rows:
                    cursor = row["seq"]
                    # Streaming is text-only. Screens and file bodies have authenticated separate routes.
                    data = {"seq": cursor, "kind": row["kind"], "created": row["created"]}
                    yield f"id: {cursor}\ndata: {json.dumps(data)}\n\n"
                yield ": heartbeat\n\n"
                await asyncio.sleep(2)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/jobs/{job_id}/report")
    async def report(job_id: str):
        job = store.job(job_id)
        if job["report"] is None:
            raise HTTPException(409, "Report is not ready")
        return {"id": job_id, "status": job["status"], **job["report"]}

    @app.get("/api/jobs/{job_id}/screen")
    async def screen(job_id: str):
        store.job(job_id)
        for action in reversed(store.actions(job_id, 30)):
            data = (action["result"] or {}).get("data", {})
            image = data.get("screen") or data.get("browser", {}).get("screen")
            if image:
                return Response(
                    base64.b64decode(image["jpeg"], validate=True),
                    media_type="image/jpeg",
                    headers={"Cache-Control": "no-store", "X-Observation-Id": action["id"]},
                )
        raise HTTPException(404, "No screenshot collected yet")

    @app.get("/api/jobs/{job_id}/artifacts/{action_id}")
    async def artifact(job_id: str, action_id: str):
        store.job(job_id)
        for action in store.actions(job_id):
            if action["id"] == action_id and action["result"] and action["result"]["ok"]:
                artifact = action["result"]["data"].get("artifact")
                if artifact:
                    return Response(
                        base64.b64decode(artifact["content_b64"], validate=True),
                        media_type="application/octet-stream",
                        headers={
                            "Content-Disposition": "attachment; filename=artifact.bin",
                            "Cache-Control": "no-store",
                        },
                    )
        raise HTTPException(404, "Artifact not found")

    static = Path(__file__).parent / "static"
    if (static / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

        @app.get("/")
        async def index():
            return FileResponse(static / "index.html", headers={"Cache-Control": "no-store"})
    else:

        @app.get("/")
        async def no_panel():
            return {"service": "extreme-autopilot", "panel": "Build web/ first; see README"}

    return app

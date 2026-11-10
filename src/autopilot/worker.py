import asyncio
import json
import logging
import sqlite3
import ssl
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx
from filelock import FileLock

from autopilot.config import token_configured
from autopilot.executor import Executor
from autopilot.schema import Action, ToolResult

log = logging.getLogger(__name__)


class Journal:
    def __init__(self, path):
        self.path = path
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS journal(id TEXT PRIMARY KEY,result TEXT)")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self, action_id):
        with self.db() as db:
            row = db.execute("SELECT result FROM journal WHERE id=?", (action_id,)).fetchone()
            if row:
                return (
                    json.loads(row[0])
                    if row[0]
                    else ToolResult(
                        ok=False,
                        unknown=True,
                        error="Worker stopped after journaling execution; side-effect outcome is unknown",
                    ).model_dump()
                )
            db.execute("INSERT INTO journal VALUES(?,NULL)", (action_id,))
        return None

    def finish(self, action_id, result):
        with self.db() as db:
            db.execute("UPDATE journal SET result=? WHERE id=?", (json.dumps(result), action_id))

    def prepare(self, action_id, redelivery=False):
        cached = self.begin(action_id)
        if cached is None and redelivery:
            cached = ToolResult(
                ok=False,
                unknown=True,
                error="Redelivered command has no local journal; outcome unknown, not replayed",
            ).model_dump()
            self.finish(action_id, cached)
        return cached


async def run_worker(settings):
    if not settings.isolated_worker:
        raise ValueError("Worker requires EA_ISOLATED_WORKER=true on a dedicated VM or machine")
    if not token_configured(settings.worker_token):
        raise ValueError("EA_WORKER_TOKEN must contain at least 32 characters")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(settings.data_dir / "worker.lock", timeout=0)
    lock.acquire()
    pool = executor = None
    current_job = None
    try:
        journal = Journal(settings.data_dir / "worker.sqlite")
        verify = ssl.create_default_context(cafile=settings.ca_file) if settings.ca_file else True
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="desktop-owner")
        async with httpx.AsyncClient(
            base_url=settings.server_url,
            verify=verify,
            timeout=20,
            trust_env=False,
            follow_redirects=False,
            headers={
                "Authorization": "Bearer " + settings.worker_token.get_secret_value(),
                "X-Worker-Session": uuid.uuid4().hex,
            },
        ) as client:
            while True:
                try:
                    response = await client.post("/worker/poll")
                    response.raise_for_status()
                    payload = response.json()
                    if payload.get("protocol") != 2:
                        raise ValueError(
                            "Unsupported coordinator protocol; upgrade coordinator and worker together"
                        )
                    command = payload["command"]
                    if not command:
                        await asyncio.sleep(2)
                        continue
                    cached = journal.prepare(command["id"], command.get("redelivery", False))
                    if cached is None:
                        try:
                            action = Action.model_validate(command["action"])
                            heartbeat = await client.get(
                                "/worker/heartbeat", params={"job_id": command["job_id"]}
                            )
                            heartbeat.raise_for_status()
                            lease = heartbeat.json()
                            if (
                                lease["status"] not in {"running", "recovering"}
                                or time.time() >= lease["deadline"]
                            ):
                                raise ValueError("Task stopped before execution")
                            if current_job != command["job_id"]:
                                if executor:
                                    await asyncio.get_running_loop().run_in_executor(pool, executor.close)
                                executor = Executor(
                                    settings, settings.data_dir / "workspaces" / command["job_id"]
                                )
                                current_job = command["job_id"]
                            executor.cancel.clear()
                        except (httpx.HTTPError, ValueError, KeyError, OSError) as exc:
                            cached = ToolResult(
                                ok=False, error="Command rejected before input: " + type(exc).__name__
                            ).model_dump()
                        if cached is None:
                            future = asyncio.get_running_loop().run_in_executor(
                                pool, executor.execute, command["prompt"], action
                            )
                            while not future.done():
                                done, _ = await asyncio.wait({future}, timeout=3)
                                if done:
                                    break
                                try:
                                    heartbeat = await client.get(
                                        "/worker/heartbeat", params={"job_id": current_job}
                                    )
                                    heartbeat.raise_for_status()
                                    lease = heartbeat.json()
                                    if (
                                        lease["status"] not in {"running", "recovering"}
                                        or time.time() >= lease["deadline"]
                                    ):
                                        executor.cancel.set()
                                except (httpx.HTTPError, ValueError, KeyError):
                                    executor.cancel.set()
                            cached = (await future).model_dump()
                        journal.finish(command["id"], cached)
                    response = await client.post(
                        "/worker/result", json={"action_id": command["id"], "result": cached}
                    )
                    response.raise_for_status()
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    reason = str(exc)[:300] if isinstance(exc, ValueError) else type(exc).__name__
                    log.warning("Worker connection/command error: %s", reason)
                    await asyncio.sleep(3)
    finally:
        try:
            if executor:
                executor.cancel.set()
                await asyncio.get_running_loop().run_in_executor(pool, executor.close)
        finally:
            try:
                if pool:
                    pool.shutdown(wait=True, cancel_futures=True)
            finally:
                lock.release()

"""Transactions fence work, budget reservations and acknowledgements across restarts."""

import json
import math
import re
import sqlite3
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from filelock import FileLock

from autopilot.schema import TERMINAL, JobInput


def uid():
    return uuid.uuid4().hex


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with FileLock(str(path) + ".schema.lock", timeout=15), ExitStack() as stack:
            probe = sqlite3.connect(path)
            try:
                worker_columns = {r[1] for r in probe.execute("PRAGMA table_info(worker)")}
                job_columns = {r[1] for r in probe.execute("PRAGMA table_info(jobs)")}
            finally:
                probe.close()
            legacy = (worker_columns and "session" not in worker_columns) or (
                job_columns and "request_key" not in job_columns
            )
            if legacy:
                stack.enter_context(FileLock(path.parent / "coordinator.lock", timeout=0))
            self._initialize()

    def _initialize(self):
        with self.tx() as db:
            db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, prompt TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, deadline REAL NOT NULL, budget REAL NOT NULL,
                spent REAL NOT NULL DEFAULT 0, reserved REAL NOT NULL DEFAULT 0,
                plan TEXT, report TEXT, errors INTEGER NOT NULL DEFAULT 0,
                step INTEGER NOT NULL DEFAULT 0, uncertain INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS actions (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                body TEXT NOT NULL, status TEXT NOT NULL, result TEXT, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS actions_job ON actions(job_id, created);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                kind TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL, reserve REAL NOT NULL,
                actual REAL, status TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS worker (id INTEGER PRIMARY KEY CHECK(id=1), seen REAL NOT NULL);
            """)
            columns = {r["name"] for r in db.execute("PRAGMA table_info(worker)")}
            if "session" not in columns:
                db.execute("ALTER TABLE worker ADD COLUMN session TEXT")
            columns = {r["name"] for r in db.execute("PRAGMA table_info(jobs)")}
            if "request_key" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN request_key TEXT")
                db.execute("ALTER TABLE jobs ADD COLUMN request_body TEXT")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS jobs_request_key "
                "ON jobs(request_key) WHERE request_key IS NOT NULL"
            )

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def event(db, job_id, kind, data):
        db.execute(
            "INSERT INTO events(job_id,kind,data,created) VALUES(?,?,?,?)",
            (job_id, kind, encode(data), time.time()),
        )

    def create(self, brief: JobInput, request_key=None):
        if request_key is not None and not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_key):
            raise ValueError(
                "Idempotency-Key must contain 16-128 ASCII letters, digits, hyphens or underscores"
            )
        job_id, now = uid(), time.time()
        with self.tx() as db:
            if request_key:
                existing = db.execute(
                    "SELECT id,request_body FROM jobs WHERE request_key=?", (request_key,)
                ).fetchone()
                if existing:
                    if json.loads(existing["request_body"]) != brief.model_dump():
                        raise ValueError("Idempotency-Key was already used for a different brief")
                    job_id = existing["id"]
                    # Return after this transaction closes, never open a nested write connection.
                    existing_id = job_id
                else:
                    existing_id = None
            else:
                existing_id = None
            if existing_id is None:
                db.execute(
                    "INSERT INTO jobs(id,prompt,status,created,deadline,budget,request_key,request_body) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        job_id,
                        brief.prompt,
                        "queued",
                        now,
                        now + brief.max_seconds,
                        brief.budget_usd,
                        request_key,
                        encode(brief.model_dump()),
                    ),
                )
                self.event(db, job_id, "queued", {})
        return self.job(job_id)

    def job(self, job_id):
        with self.tx() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        result = dict(row)
        for key in ("plan", "report"):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def jobs(self):
        with self.tx() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,status,created,deadline,spent,reserved,budget,step "
                    "FROM jobs ORDER BY created DESC LIMIT 100"
                )
            ]

    def recover(self):
        with self.tx() as db:
            db.execute("UPDATE jobs SET status='recovering' WHERE status IN ('running','recovering')")
            # Unknown billed requests remain charged at their reservation. Never silently release them.
            rows = db.execute("SELECT * FROM calls WHERE status='pending'").fetchall()
            for row in rows:
                db.execute(
                    "UPDATE jobs SET reserved=MAX(0,reserved-?),spent=spent+? WHERE id=?",
                    (row["reserve"], row["reserve"], row["job_id"]),
                )
                self._refresh_report(db, row["job_id"])
            db.execute("UPDATE calls SET status='unknown',actual=reserve WHERE status='pending'")

    def active(self):
        with self.tx() as db:
            row = db.execute(
                "SELECT id FROM jobs WHERE status IN ('running','recovering') ORDER BY created LIMIT 1"
            ).fetchone()
            if row is None:
                row = db.execute(
                    "SELECT id FROM jobs WHERE status='queued' ORDER BY created LIMIT 1"
                ).fetchone()
                if row:
                    db.execute("UPDATE jobs SET status='running' WHERE id=?", (row["id"],))
                    self.event(db, row["id"], "running", {})
        return self.job(row["id"]) if row else None

    def update(self, job_id, **fields):
        allowed = {"plan", "errors", "status", "uncertain"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Invalid job update")
        cooked = {k: encode(v) if k == "plan" else v for k, v in fields.items()}
        with self.tx() as db:
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row and row["status"] not in TERMINAL:
                db.execute(
                    f"UPDATE jobs SET {','.join(k + '=?' for k in cooked)} WHERE id=?",
                    (*cooked.values(), job_id),
                )

    def finish(self, job_id, status, report):
        if status not in TERMINAL:
            raise ValueError("Nonterminal outcome")
        with self.tx() as db:
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] in TERMINAL:
                return
            if (
                status == "completed"
                and db.execute(
                    "SELECT 1 FROM actions WHERE job_id=? "
                    "AND (status!='done' OR json_extract(result,'$.unknown')=1)",
                    (job_id,),
                ).fetchone()
            ):
                raise ValueError("Cannot complete a task with pending or uncertain operations")
            db.execute("UPDATE jobs SET status=?,report=? WHERE id=?", (status, encode(report), job_id))
            self._refresh_report(db, job_id)
            self.event(db, job_id, status, report)

    def actions(self, job_id, limit=None):
        with self.tx() as db:
            query = "SELECT * FROM actions WHERE job_id=? ORDER BY created DESC,rowid DESC"
            rows = db.execute(
                query + (" LIMIT ?" if limit is not None else ""),
                (job_id, limit) if limit is not None else (job_id,),
            ).fetchall()
        result = []
        for r in reversed(rows):
            d = dict(r)
            d["body"] = json.loads(d["body"])
            d["result"] = json.loads(d["result"]) if d["result"] else None
            result.append(d)
        return result

    def enqueue(self, job_id, body):
        action_id = uid()
        with self.tx() as db:
            job = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["status"] in TERMINAL:
                return None
            if db.execute("SELECT 1 FROM actions WHERE job_id=? AND status!='done'", (job_id,)).fetchone():
                raise ValueError("Only one outstanding action is allowed")
            db.execute(
                "INSERT INTO actions VALUES(?,?,?,'pending',NULL,?)",
                (action_id, job_id, encode(body), time.time()),
            )
            db.execute("UPDATE jobs SET step=step+1 WHERE id=?", (job_id,))
            self.event(db, job_id, "action", {"id": action_id, **body})
        return action_id

    @staticmethod
    def _claim_worker(db, session):
        now = time.time()
        row = db.execute("SELECT * FROM worker WHERE id=1").fetchone()
        if row and row["session"] not in {None, session} and 0 <= now - row["seen"] < 45:
            raise ValueError("Another worker session owns the active lease")
        db.execute(
            "INSERT INTO worker(id,seen,session) VALUES(1,?,?) "
            "ON CONFLICT(id) DO UPDATE SET seen=excluded.seen,session=excluded.session",
            (now, session),
        )

    def heartbeat(self, session, job_id=""):
        with self.tx() as db:
            self._claim_worker(db, session)
            row = (
                db.execute("SELECT status,deadline FROM jobs WHERE id=?", (job_id,)).fetchone()
                if job_id
                else None
            )
            if job_id and row is None:
                raise KeyError(job_id)
            return {"status": row["status"] if row else "idle", "deadline": row["deadline"] if row else None}

    def poll(self, session="inprocess"):
        with self.tx() as db:
            self._claim_worker(db, session)
            row = db.execute(
                """SELECT actions.*, jobs.prompt FROM actions JOIN jobs ON jobs.id=job_id
                WHERE actions.status!='done' AND jobs.status IN ('running','recovering')
                AND jobs.deadline>? ORDER BY actions.created LIMIT 1""",
                (time.time(),),
            ).fetchone()
            if row:
                db.execute("UPDATE actions SET status='dispatched' WHERE id=?", (row["id"],))
                return {
                    "id": row["id"],
                    "job_id": row["job_id"],
                    "prompt": row["prompt"],
                    "action": json.loads(row["body"]),
                    "redelivery": row["status"] == "dispatched",
                }
        return None

    def worker_online(self):
        with self.tx() as db:
            r = db.execute("SELECT seen FROM worker WHERE id=1").fetchone()
        return bool(r and 0 <= time.time() - r["seen"] < 20)

    def acknowledge(self, action_id, result, session=None):
        with self.tx() as db:
            if session is not None:
                self._claim_worker(db, session)
            row = db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if not row:
                raise KeyError(action_id)
            if row["status"] == "pending":
                raise ValueError("Action was not dispatched")
            if row["status"] == "done":
                if json.dumps(json.loads(row["result"]), sort_keys=True, allow_nan=False) != json.dumps(
                    result, sort_keys=True, allow_nan=False
                ):
                    raise ValueError("Conflicting repeated result")
                return
            db.execute("UPDATE actions SET status='done',result=? WHERE id=?", (encode(result), action_id))
            if result.get("unknown"):
                db.execute(
                    "UPDATE jobs SET uncertain=1,status=CASE WHEN status IN ('running','recovering') "
                    "THEN 'recovering' ELSE status END WHERE id=?",
                    (row["job_id"],),
                )
            self.event(db, row["job_id"], "result", {"id": action_id, **result})
            self._refresh_report(db, row["job_id"])

    def reserve(self, job_id, amount):
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("Reservation must be a finite nonnegative amount")
        call_id = uid()
        with self.tx() as db:
            r = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if (
                not r
                or r["status"] in TERMINAL
                or time.time() >= r["deadline"]
                or r["spent"] + r["reserved"] + amount > r["budget"]
            ):
                raise ValueError("Cloud budget unavailable")
            db.execute("UPDATE jobs SET reserved=reserved+? WHERE id=?", (amount, job_id))
            db.execute(
                "INSERT INTO calls VALUES(?,?,?,NULL,'pending',?)", (call_id, job_id, amount, time.time())
            )
        return call_id

    def settle(self, call_id, actual=None):
        if actual is not None and (not math.isfinite(actual) or actual < 0):
            raise ValueError("Settlement must be a finite nonnegative amount")
        with self.tx() as db:
            r = db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
            if not r or r["status"] != "pending":
                return
            charge = r["reserve"] if actual is None else actual
            db.execute(
                "UPDATE jobs SET reserved=MAX(0,reserved-?),spent=spent+? WHERE id=?",
                (r["reserve"], charge, r["job_id"]),
            )
            db.execute(
                "UPDATE calls SET actual=?,status=? WHERE id=?",
                (charge, "unknown" if actual is None else "settled", call_id),
            )
            self.event(db, r["job_id"], "usage", {"usd": charge, "estimated": actual is None})
            self._refresh_report(db, r["job_id"])

    @staticmethod
    def _refresh_report(db, job_id):
        job = db.execute("SELECT report,spent,reserved FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job or not job["report"]:
            return
        report = json.loads(job["report"])
        report.update(spent_usd=job["spent"], reserved_usd=job["reserved"])
        pending, uncertain, artifacts = [], [], []
        for row in db.execute(
            "SELECT id,status,result FROM actions WHERE job_id=? ORDER BY created,rowid", (job_id,)
        ):
            if row["status"] != "done":
                pending.append(row["id"])
            result = json.loads(row["result"]) if row["result"] else {}
            if result.get("unknown"):
                uncertain.append(row["id"])
            artifact = result.get("data", {}).get("artifact")
            if result.get("ok") and artifact:
                artifacts.append(
                    {"action_id": row["id"], "path": artifact["path"], "sha256": artifact["sha256"]}
                )
        report.update(pending_actions=pending, uncertain_actions=uncertain, artifacts=artifacts)
        db.execute("UPDATE jobs SET report=? WHERE id=?", (encode(report), job_id))

    def events(self, job_id, after=0):
        with self.tx() as db:
            return [
                {**dict(r), "data": json.loads(r["data"])}
                for r in db.execute(
                    "SELECT * FROM events WHERE job_id=? AND seq>? ORDER BY seq LIMIT 200", (job_id, after)
                )
            ]

    def recent_errors(self, job_id):
        with self.tx() as db:
            rows = db.execute(
                "SELECT data FROM events WHERE job_id=? AND kind='error' ORDER BY seq DESC LIMIT 3", (job_id,)
            ).fetchall()
        return [json.loads(row["data"]) for row in reversed(rows)]

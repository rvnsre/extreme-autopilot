"""Transactions fence work, budget reservations and acknowledgements across restarts."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from autopilot.schema import TERMINAL, JobInput


def uid():
    return uuid.uuid4().hex


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.tx() as db:
            db.executescript("""
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

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
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

    def create(self, brief: JobInput):
        job_id, now = uid(), time.time()
        with self.tx() as db:
            db.execute(
                "INSERT INTO jobs(id,prompt,status,created,deadline,budget) VALUES(?,?,?,?,?,?)",
                (job_id, brief.prompt, "queued", now, now + brief.max_seconds, brief.budget_usd),
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
            db.execute("UPDATE jobs SET status=?,report=? WHERE id=?", (status, encode(report), job_id))
            self.event(db, job_id, status, report)

    def actions(self, job_id, limit=2000):
        with self.tx() as db:
            rows = db.execute(
                "SELECT * FROM actions WHERE job_id=? ORDER BY created DESC LIMIT ?", (job_id, limit)
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

    def poll(self):
        with self.tx() as db:
            db.execute(
                "INSERT INTO worker VALUES(1,?) ON CONFLICT(id) DO UPDATE SET seen=excluded.seen",
                (time.time(),),
            )
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
        return bool(r and time.time() - r["seen"] < 20)

    def acknowledge(self, action_id, result):
        with self.tx() as db:
            row = db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if not row:
                raise KeyError(action_id)
            if row["status"] == "pending":
                raise ValueError("Action was not dispatched")
            if row["status"] == "done":
                if row["result"] != encode(result):
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

    def reserve(self, job_id, amount):
        call_id = uid()
        with self.tx() as db:
            r = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not r or r["status"] in TERMINAL or r["spent"] + r["reserved"] + amount > r["budget"]:
                raise ValueError("Cloud budget unavailable")
            db.execute("UPDATE jobs SET reserved=reserved+? WHERE id=?", (amount, job_id))
            db.execute(
                "INSERT INTO calls VALUES(?,?,?,NULL,'pending',?)", (call_id, job_id, amount, time.time())
            )
        return call_id

    def settle(self, call_id, actual=None):
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

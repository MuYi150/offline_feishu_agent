from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from ..errors import ReviewHistoryError
from ..models import PreviousIssue, ReviewHistoryRecord, ReviewOutcome


class StateStore:
    """Small transactional journal. Reads on a missing store do not create files."""

    def __init__(self, root: Path):
        self.root = root
        self.path = root / "online.sqlite"

    @contextmanager
    def connection(self):
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, key TEXT, payload TEXT NOT NULL, PRIMARY KEY(kind,key))")
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, kind, key, default=None):
        if not self.path.exists():
            return default
        with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) as connection:
            row = connection.execute("SELECT payload FROM records WHERE kind=? AND key=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else default

    def items(self, kind):
        if not self.path.exists():
            return []
        with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) as connection:
            rows = connection.execute("SELECT key,payload FROM records WHERE kind=? ORDER BY rowid", (kind,)).fetchall()
        return [(key, json.loads(value)) for key, value in rows]

    def put(self, kind, key, value):
        with self.connection() as connection:
            connection.execute("INSERT INTO records VALUES (?,?,?) ON CONFLICT(kind,key) DO UPDATE SET payload=excluded.payload",
                               (kind, key, json.dumps(value, ensure_ascii=False)))

    def add(self, kind, key, value):
        with self.connection() as connection:
            result = connection.execute("INSERT OR IGNORE INTO records VALUES (?,?,?)",
                                        (kind, key, json.dumps(value, ensure_ascii=False)))
            return result.rowcount == 1

    def control(self, node):
        return self.get("control", node, {"cycle": 0, "handoff": False})

    def pending(self, node):
        return any(j["node"] == node and j["status"] not in {"committed", "obsolete", "cancelled"}
                   for _, j in self.items("job"))

    @contextmanager
    def lock(self):
        """OS releases the lock after a crash; never unlink a potentially locked inode."""
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "run.lock").open("a+b") as handle:
            handle.seek(0, 2)
            if not handle.tell():
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("已有线上作业正在运行") from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


class LegacyHistory:
    """Only the actual V1 result and issues; no invented V2 assessments."""
    def __init__(self, value):
        self.value = value
        self.run_id = value["run_id"]
        self.review_round = value["review_round"]
        self.result = SimpleNamespace(result=ReviewOutcome(value["result"]),
                                      issues=[PreviousIssue.model_validate(x) for x in value["issues"]])

    def model_dump(self, **kwargs):
        return self.value


class OnlineHistoryStore:
    def __init__(self, store: StateStore, node: str, cycle: int):
        self.store, self.node, self.cycle = store, node, cycle

    def _records(self, document_id):
        records = self.store.get("history", f"{self.node}:{self.cycle}:{document_id}", [])
        try:
            return [LegacyHistory(x) if x.get("legacy") else ReviewHistoryRecord.model_validate(x) for x in records]
        except Exception as exc:
            raise ReviewHistoryError("线上已提交历史损坏，暂停审稿") from exc

    def load_latest(self, document_id):
        records = self._records(document_id)
        return records[-1] if records else None

    def record_count(self, document_id):
        return len(self._records(document_id))

    def load_latest_for_round(self, document_id, review_round):
        return next((x for x in reversed(self._records(document_id)) if x.review_round == review_round), None)

    def append(self, document_id, record):
        value = record.model_dump(mode="json") if hasattr(record, "model_dump") else record
        key = f"{self.node}:{self.cycle}:{document_id}"
        self._records(document_id)
        with self.store.connection() as connection:
            raw = connection.execute("SELECT payload FROM records WHERE kind='history' AND key=?", (key,)).fetchone()
            values = json.loads(raw[0]) if raw else []
            existing = next((x for x in values if x["run_id"] == value["run_id"]), None)
            if existing:
                if existing != value:
                    raise ReviewHistoryError("相同运行存在不同历史，拒绝覆盖")
                return
            values.append(value)
            connection.execute("INSERT INTO records VALUES ('history',?,?) ON CONFLICT(kind,key) DO UPDATE SET payload=excluded.payload",
                               (key, json.dumps(values, ensure_ascii=False)))

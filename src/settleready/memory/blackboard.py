"""Case Blackboard — the agent's short-term (working) memory (module 2/3).

One structured, persistent state object per case that every stage reads and
writes: extracted fields + confidence, documents received, verification
results, review-loop position. SQLite-backed (data/blackboard.db) with an
append-only audit log of every stage transition — the same log the Streamlit
Audit tab and LangSmith traces render.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    loss_type TEXT NOT NULL DEFAULT '',
    title_state TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL DEFAULT 'created',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    case_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (case_id, key)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    stage TEXT NOT NULL,
    event TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}'
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CaseBlackboard:
    """SQLite-backed case state with typed read/write and an audit log."""

    def __init__(self, db_path: str = "data/blackboard.db") -> None:
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._db = sqlite3.connect(db_path)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # -- cases -----------------------------------------------------------
    def create_case(self, case_id: str, loss_type: str = "", title_state: str = "") -> None:
        now = _now()
        self._db.execute(
            """INSERT OR IGNORE INTO cases
               (case_id, loss_type, title_state, stage, created_at, updated_at)
               VALUES (?, ?, ?, 'created', ?, ?)""",
            (case_id, loss_type, title_state, now, now),
        )
        self._db.commit()
        self.log(case_id, "created", "case_created",
                 {"loss_type": loss_type, "title_state": title_state})

    def set_stage(self, case_id: str, stage: str, event: str = "stage_transition",
                  details: dict | None = None) -> None:
        self._db.execute(
            "UPDATE cases SET stage=?, updated_at=? WHERE case_id=?",
            (stage, _now(), case_id),
        )
        self._db.commit()
        self.log(case_id, stage, event, details or {})

    def get_case(self, case_id: str) -> dict | None:
        row = self._db.execute(
            "SELECT case_id, loss_type, title_state, stage, created_at, updated_at "
            "FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            return None
        return dict(zip(
            ("case_id", "loss_type", "title_state", "stage", "created_at", "updated_at"), row))

    def list_cases(self) -> list[dict]:
        """All cases, oldest first — the dynamic case registry.

        The UI lists cases from here, not from a static fixture file, so the
        app starts empty and fills as the intake watcher creates cases."""
        rows = self._db.execute(
            "SELECT case_id, loss_type, title_state, stage, created_at, updated_at "
            "FROM cases ORDER BY created_at").fetchall()
        cols = ("case_id", "loss_type", "title_state", "stage", "created_at", "updated_at")
        return [dict(zip(cols, r)) for r in rows]

    # -- key/value working state ------------------------------------------
    def write(self, case_id: str, key: str, value) -> None:
        """Write a stage output (fields, verification, ...) as JSON."""
        self._db.execute(
            "INSERT OR REPLACE INTO kv (case_id, key, value) VALUES (?, ?, ?)",
            (case_id, key, json.dumps(value)),
        )
        self._db.commit()

    def read(self, case_id: str, key: str | None = None):
        """Read one key, or the whole working-state dict when key is None."""
        if key is not None:
            row = self._db.execute(
                "SELECT value FROM kv WHERE case_id=? AND key=?", (case_id, key)).fetchone()
            return json.loads(row[0]) if row else None
        rows = self._db.execute(
            "SELECT key, value FROM kv WHERE case_id=?", (case_id,)).fetchall()
        return {k: json.loads(v) for k, v in rows}

    # -- audit --------------------------------------------------------------
    def log(self, case_id: str, stage: str, event: str, details: dict | None = None) -> None:
        self._db.execute(
            "INSERT INTO audit (case_id, ts, stage, event, details) VALUES (?, ?, ?, ?, ?)",
            (case_id, _now(), stage, event, json.dumps(details or {})),
        )
        self._db.commit()

    def audit_log(self, case_id: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT ts, stage, event, details FROM audit WHERE case_id=? ORDER BY id",
            (case_id,)).fetchall()
        return [{"ts": ts, "stage": stage, "event": event, "details": json.loads(d)}
                for ts, stage, event, d in rows]

    def close(self) -> None:
        self._db.close()

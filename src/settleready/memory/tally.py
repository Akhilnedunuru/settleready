"""Return tally store — episodic long-term memory: learning without retraining.

Every reviewer return is logged against (reason_code, field, title_state).
When a tally crosses the promotion threshold, the check becomes a
**mandatory upfront verification** run at the start of the Verify stage —
e.g. repeated undeclared-lien returns in Georgia mean every Georgia case
verifies liens first.

Schema:
  return_tallies(reason_code, field, title_state, count, first_seen, last_seen, promoted)
  mandatory_checks(title_state, check_id, description, promoted_at)

Promotion threshold: 3 returns, same state, same reason.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

PROMOTION_THRESHOLD = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS return_tallies (
    reason_code TEXT NOT NULL,
    field TEXT NOT NULL,
    title_state TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    promoted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (reason_code, field, title_state)
);
CREATE TABLE IF NOT EXISTS mandatory_checks (
    title_state TEXT NOT NULL,
    check_id TEXT NOT NULL,
    description TEXT NOT NULL,
    promoted_at TEXT NOT NULL,
    PRIMARY KEY (title_state, check_id)
);
"""

# Maps a return reason to the concrete upfront check it promotes.
REASON_TO_CHECK = {
    "UNDECLARED_LIEN": ("verify_lienholder_first",
                        "Verify lienholder record before the standard fan-out"),
    "LIENHOLDER_MISMATCH": ("verify_lienholder_first",
                            "Verify lienholder record before the standard fan-out"),
    "WEAK_COMPS": ("widen_comparable_radius",
                   "Pull valuation comparables with a wider radius first"),
    "MISSING_TITLE_DOC": ("request_title_upfront",
                          "Request the title document in the first customer email"),
}


class ReturnTallyStore:
    """SQLite-backed tally store. `db_path` defaults to data/tally.db."""

    def __init__(self, db_path: str = "data/tally.db") -> None:
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._db = sqlite3.connect(db_path)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def log_return(self, *, reason_code: str, field: str, title_state: str) -> bool:
        """Log one reviewer return.

        Returns True if this return crossed the promotion threshold
        (i.e. a new mandatory check was just registered).
        """
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = self._db.cursor()
        cur.execute(
            """INSERT INTO return_tallies
               (reason_code, field, title_state, count, first_seen, last_seen, promoted)
               VALUES (?, ?, ?, 1, ?, ?, 0)
               ON CONFLICT(reason_code, field, title_state) DO UPDATE SET
                 count = count + 1, last_seen = excluded.last_seen""",
            (reason_code, field, title_state, now, now),
        )
        row = cur.execute(
            "SELECT count, promoted FROM return_tallies WHERE reason_code=? AND field=? AND title_state=?",
            (reason_code, field, title_state),
        ).fetchone()
        count, promoted = row
        just_promoted = False
        if count >= PROMOTION_THRESHOLD and not promoted:
            check_id, description = REASON_TO_CHECK.get(
                reason_code, (f"extra_verify_{field}", f"Extra verification for {field}"))
            cur.execute(
                """INSERT OR IGNORE INTO mandatory_checks
                   (title_state, check_id, description, promoted_at)
                   VALUES (?, ?, ?, ?)""",
                (title_state, check_id, description, now),
            )
            cur.execute(
                "UPDATE return_tallies SET promoted=1 WHERE reason_code=? AND field=? AND title_state=?",
                (reason_code, field, title_state),
            )
            just_promoted = True
        self._db.commit()
        return just_promoted

    def upfront_checks(self, title_state: str) -> list[dict]:
        """Promoted mandatory checks for this state — run before the verify fan-out."""
        cur = self._db.cursor()
        rows = cur.execute(
            "SELECT check_id, description FROM mandatory_checks WHERE title_state=? ORDER BY check_id",
            (title_state,),
        ).fetchall()
        return [{"check_id": cid, "description": desc} for cid, desc in rows]

    def tallies(self) -> list[dict]:
        """All tally rows (for the audit UI / eval)."""
        cur = self._db.cursor()
        rows = cur.execute(
            "SELECT reason_code, field, title_state, count, promoted FROM return_tallies ORDER BY count DESC"
        ).fetchall()
        return [
            {"reason_code": rc, "field": f, "title_state": s, "count": c, "promoted": bool(p)}
            for rc, f, s, c, p in rows
        ]

    def close(self) -> None:
        self._db.close()

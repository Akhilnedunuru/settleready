"""Return tally store — episodic long-term memory: learning without retraining.

Every reviewer return is logged against (reason_code, field, title_state).
When a tally crosses the promotion threshold (3), the check becomes a
**mandatory upfront verification** run at the start of the Verify stage —
e.g. repeated undeclared-lien returns in Georgia mean every Georgia case
verifies liens first.

Schema: return_tallies(reason_code, field, title_state, count,
                       first_seen, last_seen, promoted)
         mandatory_checks(title_state, check_id, description)

Test story (module 5): seed 2 GA lien tallies -> lien check NOT upfront;
add the 3rd return -> rerun -> lien check runs FIRST, visible in the audit log.
"""
from __future__ import annotations


class ReturnTallyStore:
    """TODO(module 3): SQLite implementation of the tally + promotion rule."""

    def __init__(self, db_path: str = "data/tally.db") -> None:
        raise NotImplementedError

    def log_return(self, *, reason_code: str, field: str, title_state: str) -> bool:
        """Log a return; return True if this promotion crossed the threshold."""
        raise NotImplementedError

    def upfront_checks(self, title_state: str) -> list[dict]:
        """Promoted mandatory checks for this state, run before the fan-out."""
        raise NotImplementedError

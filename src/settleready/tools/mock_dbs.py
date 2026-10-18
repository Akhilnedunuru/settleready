"""Mock external systems (module 1/3).

Every mock implements a narrow interface so a real connector can replace it
via configuration later — no agent code changes. JSON-backed; seed data comes
from scripts/seed.sh (deterministic, synthetic).
"""
from __future__ import annotations

import json
import os


class _JsonDB:
    def __init__(self, data_dir: str, filename: str) -> None:
        path = os.path.join(data_dir, filename)
        try:
            with open(path) as f:
                self._records = json.load(f)
        except FileNotFoundError:
            self._records = {}


class MockStateTitleDB(_JsonDB):
    """State title registry: brands (salvage/flood/...) per VIN."""

    def __init__(self, data_dir: str = "data") -> None:
        super().__init__(data_dir, "title_db.json")

    def lookup(self, vin: str) -> dict | None:
        """-> {"state": "TX", "brands": [...]} or None if unknown VIN."""
        return self._records.get(vin)


class MockLienholderAPI(_JsonDB):
    """Lienholder file: outstanding auto loans per VIN."""

    def __init__(self, data_dir: str = "data") -> None:
        super().__init__(data_dir, "liens.json")

    def lien_for(self, vin: str) -> dict | None:
        """-> {"holder": str, "amount": int} or None if lien-free."""
        return self._records.get(vin)


class MockClueReports(_JsonDB):
    """CLUE (loss history): prior claims per policy number."""

    def __init__(self, data_dir: str = "data") -> None:
        super().__init__(data_dir, "clue.json")

    def history(self, policy_id: str) -> list[dict]:
        """-> [prior-claim dicts], [] if clean history."""
        return self._records.get(policy_id, [])


class MockValuationService(_JsonDB):
    """Comparable listings per case (seeded with the fixtures)."""

    def __init__(self, data_dir: str = "data") -> None:
        super().__init__(data_dir, "comps.json")

    def comparables(self, case_id: str) -> list[dict]:
        """-> [{"price": int, "mileage": int, ...}], [] if unknown case."""
        return self._records.get(case_id, [])

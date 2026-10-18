"""Mock external systems (module 1/3).

Every mock implements a narrow interface so a real connector can replace it
via configuration later — no agent code changes.

Mocks: StateTitleDB (brands/liens), LienholderAPI, ClueReports, ValuationService.
Seed data comes from scripts/seed.sh (deterministic, synthetic).
"""
from __future__ import annotations


class MockStateTitleDB:
    """TODO(module 3): lookup(vin) -> {brands: [...], liens: [...]} from data/title_db.json."""

    def lookup(self, vin: str) -> dict:
        raise NotImplementedError


class MockLienholderAPI:
    """TODO(module 3): lien_for(vin) -> {amount, holder} from data/liens.json."""

    def lien_for(self, vin: str) -> dict:
        raise NotImplementedError


class MockClueReports:
    """TODO(module 3): history(policy_id) -> [prior claims] from data/clue.json."""

    def history(self, policy_id: str) -> list[dict]:
        raise NotImplementedError


class MockValuationService:
    """TODO(module 3): comparables(year, make, model, zip) -> [listings] from data/comps.json."""

    def comparables(self, *, year: int, make: str, model: str, zip_code: str) -> list[dict]:
        raise NotImplementedError

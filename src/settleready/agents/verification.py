"""Verification agent (module 3/4).

Fans out to the title DB, lienholder API, CLUE, and completeness checks, then
runs conflict resolution: extracted vs. verified — discrepancies become
exception flags with reason codes (the same codes the reviewer uses on
returns, so they feed the tally store).

Conflict Resolver policy (explicit, per Checkpoint 2.1 reviewer feedback):
when a submitted document and an authoritative record disagree, BOTH values
are preserved in the conflict record — each with its source — and neither is
silently overwritten. The authoritative record supplies the value the packet
uses (registry precedence), the resolution rule applied is recorded on the
conflict, and the conflict's status stays "unresolved": a case with an
unresolved conflict is never treated as verified, and the packet surfaces
the conflict for analyst verification before any approval.

Reads promoted upfront checks from the return tally store at stage start
(episodic memory: learning without retraining).

Check statuses: pass / warn / fail / conflict / upfront.
A case with any fail/conflict is not `passed` -> routed to review/rework.
"""
from __future__ import annotations

import os

from settleready.memory.events import emit as _emit_event
from settleready.memory.tally import ReturnTallyStore
from settleready.tools.mock_dbs import (
    MockClueReports,
    MockLienholderAPI,
    MockStateTitleDB,
    MockValuationService,
)

REQUIRED_DOCS = {
    "theft": ["claim_form", "police_report", "title"],
    "fire": ["claim_form", "police_report", "title"],
    "collision": ["claim_form", "police_report", "title", "repair_estimate"],
}


def _val(fields: dict, name: str):
    f = fields.get(name) or {}
    return f.get("value")


def _src(fields: dict, name: str, default: str = "submitted documents") -> str:
    """Where an extracted value came from (document + page), for provenance."""
    return (fields.get(name) or {}).get("source") or default


def _norm_name(name) -> str:
    return " ".join(str(name or "").lower().replace(".", " ").split())


def _conflict(reason_code: str, field: str, detail: str,
              submitted: dict, authoritative: dict, resolution: str) -> dict:
    """One structured discrepancy: both values preserved, each with its
    source; the resolution rule applied; status stays unresolved until an
    analyst verifies it — it is never treated as verified information."""
    return {"reason_code": reason_code, "field": field, "detail": detail,
            "submitted": submitted, "authoritative": authoritative,
            "resolution": resolution, "status": "unresolved",
            "requires_analyst_verification": True}


def verify_case(case_id: str, fields: dict, docs_present: list[str],
                data_dir: str = "data") -> dict:
    """Run the verification fan-out.

    Args:
        case_id: e.g. "TL-2026-1043".
        fields: extraction output {field: {"value", "confidence", "source", ...}}.
        docs_present: ["claim_form", "police_report", ...] from the intake stage.
        data_dir: fixture dir (mock DBs + tally.db live here).

    Returns:
        {"case_id", "checks": [...], "conflicts": [...], "flags": [...], "passed": bool}
    """
    title_state = _val(fields, "title_state")
    vin = _val(fields, "vin")
    policy_number = _val(fields, "policy_number")
    declared_lien = _val(fields, "lien_declared")
    loss_type = str(_val(fields, "loss_type") or "").lower()

    checks: list[dict] = []
    conflicts: list[dict] = []
    flags: list[str] = []

    # 0. Promoted upfront checks (episodic memory) run before the fan-out.
    tally = ReturnTallyStore(os.path.join(data_dir, "tally.db"))
    for c in tally.upfront_checks(title_state or ""):
        checks.append({"check": c["check_id"], "status": "upfront",
                       "detail": f"Promoted from return history: {c['description']}"})
    tally.close()

    # 1. Title registry.
    title = MockStateTitleDB(data_dir).lookup(vin) if vin else None
    _emit_event(data_dir, case_id, "tool_call",
                {"tool": "MockStateTitleDB.lookup", "args": {"vin": vin},
                 "result": (f"{title['state']} title, brands: {title.get('brands') or 'none'}"
                            if title else "no record")})
    if title is None:
        checks.append({"check": "title_lookup", "status": "fail",
                       "detail": f"No title record for VIN {vin}"})
        flags.append("title_lookup_failed")
    else:
        brands = title.get("brands") or []
        checks.append({"check": "title_lookup", "status": "pass",
                       "detail": f"{title.get('state')} title; brands: {brands or 'none'}"})
        if brands:
            flags.append("title_brand_present")

    # 2. Lienholder — Conflict Resolver: submitted documents vs. the
    # authoritative registry. Both values are preserved with their sources;
    # the registry supplies the packet value, but the discrepancy stays
    # "unresolved" and flagged for analyst verification — never verified.
    lien = MockLienholderAPI(data_dir).lien_for(vin) if vin else None
    _emit_event(data_dir, case_id, "tool_call",
                {"tool": "MockLienholderAPI.lien_for", "args": {"vin": vin},
                 "result": (f"{lien['holder']} ${lien['amount']:,}"
                            if lien else "no lien on record")})
    declared_holder = _val(fields, "lien_holder")
    form_src = _src(fields, "lien_holder", _src(fields, "lien_declared"))
    registry_val = (f"{lien['holder']} ${lien['amount']:,}" if lien
                    else "no lien on record")
    if lien and declared_lien is False:
        conflicts.append(_conflict(
            "UNDECLARED_LIEN", "lien_status",
            f"Claimant declared no lien; registry shows {registry_val}",
            submitted={"value": "no lien declared", "source": form_src},
            authoritative={"value": registry_val, "source": "lienholder registry"},
            resolution=("Registry value used for the payoff figure; the "
                        "submitted 'no lien' is preserved here, not overwritten. "
                        "Claimant confirmation requested; analyst verification "
                        "required before approval.")))
        checks.append({"check": "lien_check", "status": "conflict",
                       "detail": conflicts[-1]["detail"]})
    elif not lien and declared_lien is True:
        conflicts.append(_conflict(
            "PHANTOM_LIEN", "lien_status",
            "Claimant declared a lien; registry shows none",
            submitted={"value": f"lien declared ({declared_holder or 'holder not named'})",
                       "source": form_src},
            authoritative={"value": registry_val, "source": "lienholder registry"},
            resolution=("No payoff is deducted on an unregistered lien; the "
                        "declaration is preserved here, not overwritten. "
                        "Claimant confirmation requested; analyst verification "
                        "required before approval.")))
        checks.append({"check": "lien_check", "status": "conflict",
                       "detail": conflicts[-1]["detail"]})
    elif lien and declared_holder and _norm_name(declared_holder) != _norm_name(lien["holder"]):
        conflicts.append(_conflict(
            "LIENHOLDER_MISMATCH", "lien_holder",
            (f"Claim form names lienholder '{declared_holder}'; registry shows "
             f"'{lien['holder']}'"),
            submitted={"value": declared_holder, "source": form_src},
            authoritative={"value": registry_val, "source": "lienholder registry"},
            resolution=("Registry holder used for the payoff; the form's "
                        "lienholder is preserved here, not overwritten. The two "
                        "may both be real (e.g. a refinance the registry has "
                        "not caught up with) — analyst verification required "
                        "before approval.")))
        checks.append({"check": "lien_check", "status": "conflict",
                       "detail": conflicts[-1]["detail"]})
    elif lien:
        checks.append({"check": "lien_check", "status": "pass",
                       "detail": f"Lien verified: {lien['holder']} ${lien['amount']:,} "
                                 f"(declared: {declared_lien})"})
    else:
        checks.append({"check": "lien_check", "status": "pass",
                       "detail": "No lien on record; none declared"})

    # 3. CLUE loss history.
    priors = MockClueReports(data_dir).history(policy_number) if policy_number else []
    _emit_event(data_dir, case_id, "tool_call",
                {"tool": "MockClueReports.history",
                 "args": {"policy_number": policy_number},
                 "result": f"{len(priors)} prior claim(s)"})
    checks.append({"check": "clue_lookup",
                   "status": "warn" if priors else "pass",
                   "detail": f"{len(priors)} prior claim(s) on policy {policy_number}"})
    if priors:
        flags.append("prior_claims_present")

    # 4. Completeness: required documents for this loss type.
    required = REQUIRED_DOCS.get(loss_type, REQUIRED_DOCS["theft"])
    missing = [d for d in required if d not in docs_present]
    if missing:
        checks.append({"check": "completeness", "status": "fail",
                       "detail": f"Missing required documents: {missing}"})
        flags.append("missing_documents")
    else:
        checks.append({"check": "completeness", "status": "pass",
                       "detail": f"All required documents present: {required}"})

    # 5. Valuation data: the ACV stage needs comparable listings. A real
    # valuation API can return zero comps (rare vehicle, thin market) — that
    # must route to analyst follow-up, not crash the pipeline.
    comps = MockValuationService(data_dir).comparables(case_id)
    _emit_event(data_dir, case_id, "tool_call",
                {"tool": "MockValuationService.comparables",
                 "args": {"case_id": case_id},
                 "result": f"{len(comps)} comparable listing(s)"})
    if not comps:
        checks.append({"check": "valuation_data", "status": "fail",
                       "detail": "No comparable listings found for valuation"})
        flags.append("no_comparables")
    else:
        checks.append({"check": "valuation_data", "status": "pass",
                       "detail": f"{len(comps)} comparable listings available"})

    passed = not any(c["status"] in ("fail", "conflict") for c in checks)
    return {"case_id": case_id, "checks": checks, "conflicts": conflicts,
            "flags": flags, "passed": passed,
            # Any preserved discrepancy must be verified by the analyst; the
            # packet is assembled as "prepared — conflicts pending", never as
            # verified, while this is true.
            "requires_analyst_verification": bool(conflicts)}


if __name__ == "__main__":
    import argparse
    import json as _json

    p = argparse.ArgumentParser(description="Verify extracted fields against mock DBs")
    p.add_argument("--fields", required=True, help="JSON file: extraction fields output")
    p.add_argument("--docs", required=True, help="comma-separated docs present, e.g. claim_form,police_report")
    p.add_argument("--case", required=True)
    p.add_argument("--data", default="data")
    a = p.parse_args()

    with open(a.fields) as f:
        fields = _json.load(f)
    report = verify_case(a.case, fields, a.docs.split(","), data_dir=a.data)
    print(f"Case {a.case}: passed={report['passed']}")
    for c in report["checks"]:
        print(f"  [{c['status']:8s}] {c['check']:14s} {c['detail']}")
    for cf in report["conflicts"]:
        print(f"  CONFLICT {cf['reason_code']} ({cf['field']}): {cf['detail']}")
    print("flags:", report["flags"])

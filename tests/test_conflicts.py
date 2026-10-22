"""Conflict Resolver (Checkpoint 2.1 reviewer feedback).

When a submitted document and an authoritative record disagree, the agent
preserves BOTH values with their sources, records the resolution rule, keeps
the conflict status "unresolved", and flags the case as requiring analyst
verification — an unresolved discrepancy is never treated as verified.
"""
import json
import os

from settleready.agents.outreach import needs_from_report, needs_from_return
from settleready.agents.verification import verify_case
from settleready.data.seed import main as seed_main


def _spec(data_dir, case_id):
    with open(os.path.join(data_dir, "cases.json")) as f:
        return next(c for c in json.load(f) if c["case_id"] == case_id)


def _fields(spec, **over):
    f = {
        "vin": {"value": spec["vehicle"]["vin"], "confidence": 1.0,
                "source": "claim_form.pdf p.1"},
        "title_state": {"value": spec["title_state"], "confidence": 1.0,
                        "source": "claim_form.pdf p.1"},
        "policy_number": {"value": spec["policy"]["number"], "confidence": 1.0,
                          "source": "claim_form.pdf p.1"},
        "lien_declared": {"value": spec["declared_lien"], "confidence": 1.0,
                          "source": "claim_form.pdf p.2"},
        "loss_type": {"value": spec["loss_type"], "confidence": 1.0,
                      "source": "claim_form.pdf p.1"},
    }
    f.update(over)
    return f


def test_lienholder_mismatch_preserves_both_values(tmp_path):
    seed_main(seed=42, out=str(tmp_path), num_cases=3)
    spec = _spec(str(tmp_path), "TL-2026-1045")  # registry: Lone Star CU $6,750
    fields = _fields(spec, lien_holder={
        "value": "Capital Auto Finance", "confidence": 0.97,
        "source": "claim_form.pdf p.2"})
    out = verify_case("TL-2026-1045", fields, spec["documents"],
                      data_dir=str(tmp_path))
    assert out["passed"] is False
    assert out["requires_analyst_verification"] is True
    (cf,) = [c for c in out["conflicts"] if c["reason_code"] == "LIENHOLDER_MISMATCH"]
    assert cf["submitted"]["value"] == "Capital Auto Finance"
    assert cf["submitted"]["source"] == "claim_form.pdf p.2"
    assert "Lone Star Credit Union" in cf["authoritative"]["value"]
    assert cf["authoritative"]["source"] == "lienholder registry"
    assert cf["status"] == "unresolved"
    assert "not overwritten" in cf["resolution"]


def test_undeclared_lien_conflict_is_structured(tmp_path):
    seed_main(seed=42, out=str(tmp_path), num_cases=3)
    spec = _spec(str(tmp_path), "TL-2026-1043")  # registry lien, none declared
    out = verify_case("TL-2026-1043", _fields(spec), spec["documents"],
                      data_dir=str(tmp_path))
    (cf,) = [c for c in out["conflicts"] if c["reason_code"] == "UNDECLARED_LIEN"]
    assert cf["submitted"]["value"] == "no lien declared"
    assert "Capital Auto Finance" in cf["authoritative"]["value"]
    assert cf["status"] == "unresolved"
    assert out["requires_analyst_verification"] is True


def test_matching_lienholder_is_verified_no_conflict(tmp_path):
    seed_main(seed=42, out=str(tmp_path), num_cases=3)
    spec = _spec(str(tmp_path), "TL-2026-1045")
    fields = _fields(spec, lien_holder={
        "value": "Lone Star Credit Union", "confidence": 1.0,
        "source": "claim_form.pdf p.2"})
    out = verify_case("TL-2026-1045", fields, spec["documents"],
                      data_dir=str(tmp_path))
    assert out["conflicts"] == []
    assert out["requires_analyst_verification"] is False
    assert out["passed"] is True


def test_mismatch_is_customer_actionable_and_returnable(tmp_path):
    seed_main(seed=42, out=str(tmp_path), num_cases=3)
    spec = _spec(str(tmp_path), "TL-2026-1045")
    fields = _fields(spec, lien_holder={
        "value": "Capital Auto Finance", "confidence": 0.97,
        "source": "claim_form.pdf p.2"})
    out = verify_case("TL-2026-1045", fields, spec["documents"],
                      data_dir=str(tmp_path))
    needs = needs_from_report(out)
    assert any("lienholder" in n for n in needs)
    assert needs_from_return("LIENHOLDER_MISMATCH", "form says Capital Auto Finance")


def test_seed_generates_mismatch_cases_with_form_value(tmp_path):
    seed_main(seed=42, out=str(tmp_path), num_cases=15)
    with open(os.path.join(str(tmp_path), "cases.json")) as f:
        specs = json.load(f)
    mismatches = [c for c in specs if c.get("form_lien_holder")
                  and c["lien"] and c["form_lien_holder"] != c["lien"]["holder"]]
    assert len(mismatches) >= 2
    # the form actually prints the (wrong) name the claimant wrote
    from settleready.tools.pdf_parser import parse_document
    case = mismatches[0]
    pages = parse_document(os.path.join(
        str(tmp_path), "pdfs", case["case_id"], "claim_form.pdf"))
    text = " ".join(p["text"] for p in pages)
    assert case["form_lien_holder"] in text

"""Synthetic fixture generation (module 1).

`scripts/seed.sh` runs this. Generates, deterministically from --seed:

- data/cases.json ......... master case-spec schema (EVERYTHING derives from this,
                             so fixtures stay cross-consistent: VIN matches everywhere,
                             lien status agrees, comps are plausible)
- data/pdfs/<CASE>/*.pdf . claim form, title, police report / repair estimate
                             (reportlab; every page bannered SYNTHETIC)
- data/title_db.json ...... {vin: {state, brands}}
- data/liens.json ......... {vin: {holder, amount} | null}
- data/clue.json ........... {policy_number: [prior claims]}
- data/comps.json .......... {case_id: [comparable listings]}
- data/statutes/<ST>.md .... synthetic total-loss law summaries (RAG corpus)
- data/inbox.json .......... seeded claim email thread (MockEmailClient)
- data/tally.db ............ seeded return-tally history (2x GA UNDECLARED_LIEN,
                             so the promotion rule is demoable)

ACV convention (matches tools/acv.py): acv = mean(comps)
    - $150 per full 10k miles over 50k  - condition adjustment.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors

from settleready.tools.email_client import MockEmailClient
from settleready.memory.tally import ReturnTallyStore

BANNER = "SYNTHETIC DOCUMENT — FOR CAPSTONE DEMO ONLY. NOT A REAL CLAIM."

FIRST = ["Jordan", "Priya", "Marcus", "Elena", "David", "Aisha"]
LAST = ["Ellis", "Raman", "Webb", "Brooks", "Kim", "Patel"]
STREETS = ["1200 Lamar Ave", "45 Peachtree Ln", "78 Congress St", "9 Bayou Rd"]
CITIES = {"TX": ("Austin", "78701"), "GA": ("Atlanta", "30308"), "CA": ("Fresno", "93721")}


def _vin(rng: random.Random) -> str:
    chars = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"
    return "".join(rng.choice(chars) for _ in range(17))


def _name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST)} {rng.choice(LAST)}"


def _comps(rng: random.Random, base: int, n: int = 6) -> list[dict]:
    comps = []
    for i in range(n):
        comps.append({
            "id": f"CMP-{rng.randint(10000, 99999)}",
            "price": base + rng.randint(-900, 900),
            "mileage": rng.randint(30000, 95000),
            "distance_mi": rng.randint(5, 120),
            "source": rng.choice(["AutoTrader", "Cars.com", "CarGurus"]),
        })
    # nudge the last comp so the mean is exactly `base` (clean demo numbers):
    # the mean moves by delta/n, so add n * the shortfall to one comp.
    mean = sum(c["price"] for c in comps) / n
    comps[-1]["price"] += round((base - mean) * n)
    return comps


def _expected_acv(comps: list[dict], mileage: int, condition: str = "good") -> int:
    mean = sum(c["price"] for c in comps) / len(comps)
    adj = 150 * max(0, (mileage - 50000) // 10000)
    if condition == "fair":
        adj += 300
    elif condition == "poor":
        adj += 800
    return int(round(mean)) - adj


def case_specs(rng: random.Random) -> list[dict]:
    """The master schema. Edit here; everything downstream follows."""
    cases = []

    # Case 1 — the demo case (matches the architecture doc trace)
    c1_comps = _comps(rng, 18400)
    cases.append({
        "case_id": "TL-2026-1043",
        "loss_type": "theft",
        "title_state": "TX",
        "vehicle": {"year": 2019, "make": "Honda", "model": "Accord", "trim": "EX",
                    "vin": _vin(rng), "mileage": 92000, "color": "Modern Steel Metallic"},
        "policy": {"number": "TX-A-4829137", "holder": _name(rng)},
        "lien": {"holder": "Capital Auto Finance", "amount": 4200},
        "declared_lien": False,          # <-- conflict demo: claimant said "no lien"
        "title_brands": [],
        "loss": {"date": "2026-09-14",
                 "description": "Vehicle stolen overnight from apartment parking garage.",
                 "police_report_no": "APD-2026-88121", "agency": "Austin Police Dept"},
        "documents": ["claim_form", "police_report"],   # title doc MISSING -> completeness loop
        "comps": c1_comps,
        "expected_acv": _expected_acv(c1_comps, 92000),
    })

    # Case 2 — GA fire, clean (tally-demo state)
    c2_comps = _comps(rng, 22300)
    cases.append({
        "case_id": "TL-2026-1044",
        "loss_type": "fire",
        "title_state": "GA",
        "vehicle": {"year": 2021, "make": "Toyota", "model": "Camry", "trim": "LE",
                    "vin": _vin(rng), "mileage": 41000, "color": "Celestial Silver"},
        "policy": {"number": "GA-B-1172045", "holder": _name(rng)},
        "lien": None,
        "declared_lien": False,
        "title_brands": [],
        "loss": {"date": "2026-09-20",
                 "description": "Engine fire; vehicle destroyed. Fire dept report attached.",
                 "police_report_no": "AFD-2026-33209", "agency": "Atlanta Fire Rescue"},
        "documents": ["claim_form", "police_report", "title"],
        "comps": c2_comps,
        "expected_acv": _expected_acv(c2_comps, 41000),
    })

    # Case 3 — TX collision total (repair estimate path)
    c3_comps = _comps(rng, 21900)
    cases.append({
        "case_id": "TL-2026-1045",
        "loss_type": "collision",
        "title_state": "TX",
        "vehicle": {"year": 2017, "make": "Ford", "model": "F-150", "trim": "XLT",
                    "vin": _vin(rng), "mileage": 88000, "color": "Oxford White"},
        "policy": {"number": "TX-A-9033118", "holder": _name(rng)},
        "lien": {"holder": "Lone Star Credit Union", "amount": 6750},
        "declared_lien": True,
        "title_brands": [],
        "loss": {"date": "2026-09-25",
                 "description": "Single-vehicle collision with guardrail; frame damage.",
                 "police_report_no": "TXDPS-2026-77154", "agency": "TX Highway Patrol"},
        "documents": ["claim_form", "police_report", "title", "repair_estimate"],
        "comps": c3_comps,
        "expected_acv": _expected_acv(c3_comps, 88000),
        "repair_estimate_total": 19800,
    })
    return cases


# ---------------------------------------------------------------- PDFs ---

def _pdf(path: str, title: str, rows: list[tuple[str, str]]) -> None:
    styles = {
        "banner": ParagraphStyle("b", fontSize=9, textColor=colors.red, leading=12),
        "h": ParagraphStyle("h", fontSize=16, leading=20, spaceAfter=8),
        "k": ParagraphStyle("k", fontSize=10, leading=13),
        "v": ParagraphStyle("v", fontSize=10, leading=13),
    }
    story = [Paragraph(BANNER, styles["banner"]), Spacer(1, 0.15 * inch),
             Paragraph(title, styles["h"])]
    table_rows = [[Paragraph(f"<b>{k}</b>", styles["k"]), Paragraph(str(v), styles["v"])]
                  for k, v in rows]
    t = Table(table_rows, colWidths=[2.0 * inch, 4.5 * inch])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f0f0f0")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(t)
    SimpleDocTemplate(path, pagesize=letter,
                      topMargin=0.6 * inch, bottomMargin=0.6 * inch).build(story)


def _build_pdfs(case: dict, pdf_dir: str) -> None:
    v, p, loss = case["vehicle"], case["policy"], case["loss"]
    city, zipc = CITIES[case["title_state"]]
    holder_addr = f"{random.Random(7).choice(STREETS)}, {city} {zipc}"

    _pdf(os.path.join(pdf_dir, "claim_form.pdf"), "Mobile Claim Filing Record", [
        ("Case ID", case["case_id"]),
        ("Policy number", p["number"]),
        ("Policyholder", f'{p["holder"]} — {holder_addr}'),
        ("Loss date", loss["date"]),
        ("Loss type", case["loss_type"].upper()),
        ("Description", loss["description"]),
        ("Vehicle", f'{v["year"]} {v["make"]} {v["model"]} {v["trim"]}'),
        ("VIN", v["vin"]),
        ("Mileage", f'{v["mileage"]:,}'),
        ("Lien declared by claimant", "YES" if case["declared_lien"] else "NO"),
    ])

    _pdf(os.path.join(pdf_dir, "police_report.pdf"),
         f'{loss["agency"]} — Incident Report {loss["police_report_no"]}', [
             ("Report no.", loss["police_report_no"]),
             ("Agency", loss["agency"]),
             ("Date", loss["date"]),
             ("Vehicle", f'{v["year"]} {v["make"]} {v["model"]} — VIN {v["vin"]}'),
             ("Narrative", loss["description"] + " Vehicle not recovered at time of report."),
         ])

    if "title" in case["documents"]:
        lien = case["lien"]
        _pdf(os.path.join(pdf_dir, "title.pdf"),
             f'State of {case["title_state"]} — Certificate of Title', [
                 ("Title no.", f'{case["title_state"]}-T-{random.Random(9).randint(100000,999999)}'),
                 ("VIN", v["vin"]),
                 ("Owner", p["holder"]),
                 ("Title brands", ", ".join(case["title_brands"]) or "NONE"),
                 ("Lienholder", f'{lien["holder"]} — ${lien["amount"]:,}' if lien else "NONE"),
             ])

    if "repair_estimate" in case["documents"]:
        total = case["repair_estimate_total"]
        _pdf(os.path.join(pdf_dir, "repair_estimate.pdf"), "Collision Repair Estimate", [
            ("Shop", "Capstone Auto Body, Austin TX"),
            ("VIN", v["vin"]),
            ("Frame straightening", "$4,200"),
            ("Front suspension", "$2,850"),
            ("Body panels + paint", "$6,900"),
            ("Airbag module", "$2,300"),
            ("Labor (38h)", "$3,550"),
            ("TOTAL", f"${total:,}"),
        ])


# ------------------------------------------------------------- statutes ---

STATUTES = {
    "TX": ("Texas — Total Loss (synthetic summary)",
           "TOTAL LOSS THRESHOLD: In Texas, a vehicle is a total loss when the cost of "
           "repairs plus salvage value equals or exceeds 100% of the actual cash value "
           "immediately before the loss. Theft and fire with no recoverable vehicle are "
           "total losses by definition.\n\n"
           "VALUATION METHOD: ACV is determined from comparable vehicles of like kind and "
           "quality available in the local market, adjusted for mileage and condition."),
    "GA": ("Georgia — Total Loss (synthetic summary)",
           "TOTAL LOSS THRESHOLD: In Georgia, a vehicle is a total loss when repair costs "
           "exceed 75% of the fair retail value. Stolen vehicles not recovered within 30 "
           "days are total losses.\n\n"
           "VALUATION METHOD: Fair retail value from at least 5 comparable listings, "
           "adjusted for mileage over 50,000 at $150 per 10,000 miles."),
    "CA": ("California — Total Loss (synthetic summary)",
           "TOTAL LOSS THRESHOLD: California uses the total loss formula: a vehicle is a "
           "total loss when repair cost plus salvage value is greater than the actual "
           "cash value before the loss.\n\n"
           "VALUATION METHOD: ACV based on comparable vehicles in the local market area, "
           "with documented adjustments for mileage, condition, and options."),
}


def _write(path: str, obj) -> None:
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


# ----------------------------------------------------------------- main ---

def main(seed: int = 42, out: str = "data", env_only: bool = False) -> None:
    """Generate the synthetic world.

    env_only=True writes just the environment the agent operates in — the
    statute corpus and the mock title/lien/CLUE/valuation records. No demo
    cases, no PDFs, no seeded inbox, no tally history: a fresh start where
    cases are created by the intake watcher as real claim emails arrive.
    env_only=False (default) additionally writes the 3 canned demo fixtures
    (cases.json, PDFs, seeded inbox + tally) used by the eval harness and the
    scripted demo.
    """
    rng = random.Random(seed)
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "pdfs"), exist_ok=True)
    os.makedirs(os.path.join(out, "statutes"), exist_ok=True)

    cases = case_specs(rng)

    title_db, liens, clue, comps = {}, {}, {}, {}
    for case in cases:
        vin = case["vehicle"]["vin"]
        if not env_only:
            pdf_dir = os.path.join(out, "pdfs", case["case_id"])
            os.makedirs(pdf_dir, exist_ok=True)
            _build_pdfs(case, pdf_dir)

        title_db[vin] = {"state": case["title_state"], "brands": case["title_brands"]}
        liens[vin] = ({"holder": case["lien"]["holder"], "amount": case["lien"]["amount"]}
                      if case["lien"] else None)
        clue[case["policy"]["number"]] = []   # synthetic: no prior claims
        comps[case["case_id"]] = case["comps"]

    _write(os.path.join(out, "title_db.json"), title_db)
    _write(os.path.join(out, "liens.json"), liens)
    _write(os.path.join(out, "clue.json"), clue)
    _write(os.path.join(out, "comps.json"), comps)

    for st, (title, body) in STATUTES.items():
        with open(os.path.join(out, "statutes", f"{st}.md"), "w") as f:
            f.write(f"# {BANNER}\n\n# {title}\n\n{body}\n")

    if env_only:
        print(f"Environment only -> {out}/ (statutes + mock DBs, no demo fixtures)")
        return

    _write(os.path.join(out, "cases.json"), cases)

    # Seeded inbox: the demo claim email for case 1
    c1 = cases[0]
    mail = MockEmailClient(data_dir=out)
    mail.inject_inbound(
        to="claims@demo.local",
        from_=f'{c1["policy"]["holder"]} <customer@example.com>',
        subject=f'Claim filed via mobile app — {c1["case_id"]}',
        body=(f'Hello, my {c1["vehicle"]["year"]} {c1["vehicle"]["make"]} '
              f'{c1["vehicle"]["model"]} was stolen last night. '
              f'Police report {c1["loss"]["police_report_no"]} attached.\n'
              f'Attachments: claim_form.pdf, police_report.pdf'),
    )

    # Seeded tally history: 2x GA UNDECLARED_LIEN (one more promotes the check)
    tally = ReturnTallyStore(db_path=os.path.join(out, "tally.db"))
    for _ in range(2):
        tally.log_return(reason_code="UNDECLARED_LIEN", field="lien_status", title_state="GA")
    tally.close()

    print(f"Seeded {len(cases)} cases -> {out}/")
    for c in cases:
        print(f'  {c["case_id"]}: {c["title_state"]} {c["loss_type"]}, '
              f'expected ACV ${c["expected_acv"]:,}, docs={c["documents"]}')


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default="data")
    p.add_argument("--env-only", action="store_true",
                   help="only the environment (statutes + mock DBs); no demo fixtures")
    a = p.parse_args()
    main(seed=a.seed, out=a.out, env_only=a.env_only)

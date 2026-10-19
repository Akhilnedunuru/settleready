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


def case_specs(rng: random.Random, n: int = 3) -> list[dict]:
    """The master schema. Edit here; everything downstream follows.

    The first 3 are the hand-designed fixtures (eval ground truth + scripted
    demo) and never change. Beyond that, deterministic generated cases cycle
    through archetypes: clean (→ ready_for_review), undeclared lien
    (→ needs_info via conflict), missing title doc (→ needs_info via
    completeness). Case IDs continue TL-2026-1046, ...
    """
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

    for i in range(3, max(3, n)):
        cases.append(_generated_case(rng, i))
    return cases


# --------------------------------------------------- generated cases ---

_VEHICLES = [
    (2020, "Honda", "Civic", "LX"), (2018, "Toyota", "RAV4", "XLE"),
    (2022, "Ford", "Escape", "SE"), (2019, "Chevrolet", "Malibu", "LT"),
    (2021, "Nissan", "Altima", "SV"), (2017, "Hyundai", "Elantra", "SEL"),
    (2020, "Jeep", "Wrangler", "Sport"), (2019, "Subaru", "Outback", "Premium"),
    (2022, "Kia", "K5", "LXS"), (2018, "Mazda", "CX-5", "Touring"),
    (2021, "Volkswagen", "Jetta", "S"), (2019, "GMC", "Sierra 1500", "SLE"),
]
_GEN_LOSS_TYPES = ["theft", "fire", "collision"]
_GEN_STATES = ["TX", "GA", "CA"]
_GEN_AGENCIES = {"TX": ("TXDPS", "TX Highway Patrol"),
                 "GA": ("GSP", "Georgia State Patrol"),
                 "CA": ("CHP", "California Highway Patrol")}
_GEN_PLACES = ["apartment parking garage", "grocery store parking lot",
               "driveway", "office parking structure"]
_GEN_LIENHOLDERS = ["Capital Auto Finance", "Lone Star Credit Union",
                    "Desert Federal Bank", "First Community Bank"]
_GEN_COLORS = ["Silver", "Black", "White", "Gray", "Blue", "Red"]


def _generated_case(rng: random.Random, i: int) -> dict:
    """One deterministic synthetic case. i is the 0-based index into case_specs
    (3, 4, ...); archetype cycles clean / undeclared-lien / missing-docs."""
    idx = i - 3
    loss_type = _GEN_LOSS_TYPES[idx % 3]
    state = _GEN_STATES[idx % 3]
    archetype = idx % 3  # 0: clean, 1: undeclared lien, 2: missing title doc
    year, make, model, trim = _VEHICLES[idx % len(_VEHICLES)]
    mileage = rng.randint(35000, 95000)
    base = rng.randint(16000, 26000)
    comps = _comps(rng, base)
    agency_code, agency_name = _GEN_AGENCIES[state]

    docs = ["claim_form", "police_report", "title"]
    case_extra = {}
    if loss_type == "collision":
        docs.append("repair_estimate")
        case_extra["repair_estimate_total"] = rng.randint(18500, 21500)

    lien, declared_lien = None, False
    if archetype == 1:  # conflict demo: lien on record, claimant said "no lien"
        lien = {"holder": rng.choice(_GEN_LIENHOLDERS),
                "amount": rng.randint(3000, 9000)}
        declared_lien = False
    elif archetype == 0 and rng.random() < 0.5:  # properly declared lien
        lien = {"holder": rng.choice(_GEN_LIENHOLDERS),
                "amount": rng.randint(2000, 8000)}
        declared_lien = True
    if archetype == 2:  # completeness loop: title doc missing
        docs.remove("title")

    if loss_type == "theft":
        desc = f"Vehicle stolen overnight from {rng.choice(_GEN_PLACES)}."
    elif loss_type == "fire":
        desc = "Engine compartment fire; vehicle destroyed."
    else:
        desc = "Collision with guardrail; frame and suspension damage."

    return {
        "case_id": f"TL-2026-{1043 + i}",
        "loss_type": loss_type,
        "title_state": state,
        "vehicle": {"year": year, "make": make, "model": model, "trim": trim,
                    "vin": _vin(rng), "mileage": mileage,
                    "color": rng.choice(_GEN_COLORS)},
        "policy": {"number": f"{state}-{rng.choice('ABCD')}-{rng.randint(1000000, 9999999)}",
                   "holder": _name(rng)},
        "lien": lien,
        "declared_lien": declared_lien,
        "title_brands": [],
        "loss": {"date": f"2026-09-{rng.randint(10, 28):02d}",
                 "description": desc,
                 "police_report_no": f"{agency_code}-2026-{rng.randint(10000, 99999)}",
                 "agency": agency_name},
        "documents": docs,
        "comps": comps,
        "expected_acv": _expected_acv(comps, mileage),
        **case_extra,
    }


# ---------------------------------------------------------------- PDFs ---

# ------------------------------------------------------- PDF forms ---
# Realistic claim documents, modeled on real-world forms (ACORD 2 Automobile
# Loss Notice, Texas CR-3 crash report, state certificate of title, body-shop
# repair estimate). Layouts are original; all data is synthetic.

_BANNER_PDF = BANNER.replace("\u2014", "-")


def _form_styles() -> dict:
    return {
        "doctitle": ParagraphStyle("doctitle", fontName="Helvetica-Bold",
                                   fontSize=15, leading=18, spaceAfter=2),
        "docsub": ParagraphStyle("docsub", fontSize=9, leading=11, spaceAfter=6,
                                 textColor=colors.HexColor("#555555")),
        "section": ParagraphStyle("section", fontName="Helvetica-Bold",
                                  fontSize=10, leading=13, textColor=colors.white),
        "k": ParagraphStyle("k", fontName="Helvetica-Bold", fontSize=9, leading=12),
        "v": ParagraphStyle("v", fontSize=9, leading=12),
        "body": ParagraphStyle("body", fontSize=9, leading=13, spaceAfter=6),
        "small": ParagraphStyle("small", fontSize=7.5, leading=10, spaceAfter=4,
                                textColor=colors.HexColor("#555555")),
        "center": ParagraphStyle("center", fontSize=9, leading=12, alignment=1),
    }


def _finish_doc(path: str, story: list) -> None:
    def _banner(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica-Bold", 7)
        canvas.setFillColor(colors.red)
        canvas.drawCentredString(4.25 * inch, 10.6 * inch, _BANNER_PDF)
        canvas.restoreState()
    SimpleDocTemplate(path, pagesize=letter, topMargin=0.8 * inch,
                      bottomMargin=0.6 * inch).build(
        story, onFirstPage=_banner, onLaterPages=_banner)


def _section_bar(story: list, styles: dict, text: str) -> None:
    t = Table([[Paragraph(text.upper(), styles["section"])]], colWidths=[7.5 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#2f3e55")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story += [Spacer(1, 0.14 * inch), t]


def _fields(story: list, styles: dict, rows: list[tuple[str, str]],
            label_w: float = 2.3 * inch) -> None:
    tr = [[Paragraph(f"<b>{k}</b>", styles["k"]), Paragraph(str(v), styles["v"])]
          for k, v in rows]
    t = Table(tr, colWidths=[label_w, 7.5 * inch - label_w])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef1f5")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(t)


def _sig_block(story: list, styles: dict, labels: list[str]) -> None:
    cells = []
    for lab in labels:
        cells += [Paragraph(f"X{'_' * 34}", styles["v"]),
                  Paragraph(lab, styles["small"])]
    t = Table([cells], colWidths=[2.5 * inch] * len(labels))
    t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 6)]))
    story += [Spacer(1, 0.2 * inch), t]


_OFFICERS = ["J. Martinez", "R. Thompson", "K. Okafor", "D. Nguyen", "S. Patel"]


def _case_rng(case: dict) -> random.Random:
    # deterministic cosmetic extras (plates, DL nos, badge nos) per case
    return random.Random(1000 + int(case["case_id"].rsplit("-", 1)[-1]))


def _plate(rng: random.Random, state: str) -> str:
    return f"{state} {''.join(rng.choice('ABCDEFGHJKLMNPRSTUVWXYZ') for _ in range(3))}-{rng.randint(1000, 9999)}"


def _build_fnol(case: dict, pdf_dir: str) -> None:
    """First Notice of Loss - Automobile (ACORD 2 style)."""
    v, p, loss = case["vehicle"], case["policy"], case["loss"]
    rng = _case_rng(case)
    styles = _form_styles()
    city, zipc = CITIES[case["title_state"]]
    holder_addr = f"{rng.choice(STREETS)}, {city}, {case['title_state']} {zipc}"
    phone = f"(555) 01{rng.randint(0, 9)}-{rng.randint(1000, 9999):04d}"
    plate = _plate(rng, case["title_state"])
    loss_time = f"{rng.randint(1, 12)}:{rng.randint(10, 59):02d} {rng.choice(['AM', 'PM'])}"
    driver = "Unknown - vehicle stolen" if case["loss_type"] == "theft" else p["holder"]

    story = [Paragraph("FIRST NOTICE OF LOSS - AUTOMOBILE", styles["doctitle"]),
             Paragraph("Capstone Mutual Insurance Company &nbsp;|&nbsp; Auto Claims Department",
                       styles["docsub"])]
    _section_bar(story, styles, "Report information")
    _fields(story, styles, [
        ("Date reported", loss["date"]),
        ("Reported by", f"{p['holder']} (Policyholder)"),
        ("Report method", "Mobile app"),
        ("Claim reference no.", case["case_id"]),
    ])
    _section_bar(story, styles, "Policy information")
    _fields(story, styles, [
        ("Company", "Capstone Mutual Insurance Company"),
        ("Policy number", p["number"]),
        ("Policy type", "Personal Auto"),
        ("Policy period", "01/01/2026 to 12/31/2026"),
    ])
    _section_bar(story, styles, "Insured")
    _fields(story, styles, [
        ("Name", p["holder"]),
        ("Address", holder_addr),
        ("Phone", phone),
    ])
    _section_bar(story, styles, "Loss details")
    _fields(story, styles, [
        ("Date of loss", loss["date"]),
        ("Time of loss", loss_time),
        ("Loss type", case["loss_type"].upper()),
        ("Location of loss", f"{city}, {case['title_state']}"),
        ("Description of loss", loss["description"]),
        ("Authority contacted", loss["agency"]),
        ("Authority report no.", loss["police_report_no"]),
    ])
    _section_bar(story, styles, "Insured vehicle")
    _fields(story, styles, [
        ("Year / Make / Model", f"{v['year']} {v['make']} {v['model']} {v['trim']}"),
        ("Vehicle Identification Number (VIN)", v["vin"]),
        ("License plate", plate),
        ("Mileage", f"{v['mileage']:,}"),
        ("Owner", p["holder"]),
        ("Driver at time of loss", driver),
    ])
    _section_bar(story, styles, "Lienholder")
    lien = case["lien"]
    _fields(story, styles, [
        ("Is there a lienholder on this vehicle?",
         "YES" if case["declared_lien"] else "NO"),
        ("Lienholder name", lien["holder"] if case["declared_lien"] and lien else "N/A"),
    ])
    story.append(Paragraph(
        "Fraud notice: Any person who knowingly presents a false or fraudulent claim "
        "for payment of a loss is subject to criminal and civil penalties.",
        styles["small"]))
    _sig_block(story, styles, ["Signature of insured", "Date"])
    _finish_doc(os.path.join(pdf_dir, "claim_form.pdf"), story)


def _build_police_report(case: dict, pdf_dir: str) -> None:
    """Police report. Theft/fire get an incident report; collision gets a
    CR-3 style crash report."""
    v, p, loss = case["vehicle"], case["policy"], case["loss"]
    rng = _case_rng(case)
    styles = _form_styles()
    city, zipc = CITIES[case["title_state"]]
    holder_addr = f"{rng.choice(STREETS)}, {city}, {case['title_state']} {zipc}"
    plate = _plate(rng, case["title_state"])
    officer = rng.choice(_OFFICERS)
    badge = f"{rng.randint(1000, 9999)}"
    loss_kind = case["loss_type"]

    if loss_kind == "theft":
        title, sub = "INCIDENT / OFFENSE REPORT", "Stolen Motor Vehicle"
    elif loss_kind == "fire":
        title, sub = "INCIDENT REPORT", "Vehicle Fire"
    else:
        title, sub = "PEACE OFFICER'S CRASH REPORT", "Motor Vehicle Traffic Crash"
    story = [Paragraph(title, styles["doctitle"]),
             Paragraph(f"{loss['agency']} &nbsp;|&nbsp; {sub}", styles["docsub"]),
             Paragraph(f"<b>Report no. {loss['police_report_no']}</b>", styles["center"])]

    if loss_kind == "collision":
        _section_bar(story, styles, "Crash identification")
        _fields(story, styles, [
            ("Crash date", loss["date"]),
            ("Crash time", f"{rng.randint(1, 12)}:{rng.randint(10, 59):02d} {rng.choice(['AM', 'PM'])}"),
            ("Location", f"{rng.choice(STREETS)}, {city}, {case['title_state']}"),
            ("Weather", "Clear"), ("Road condition", "Dry"), ("Light", "Daylight"),
        ])
        _section_bar(story, styles, "Unit 1 - Driver")
        _fields(story, styles, [
            ("Driver name", p["holder"]),
            ("Driver license no.", f"{case['title_state']}{rng.randint(10000000, 99999999)}"),
            ("Address", holder_addr),
        ])
        _section_bar(story, styles, "Unit 1 - Vehicle")
        _fields(story, styles, [
            ("Year / Make / Model", f"{v['year']} {v['make']} {v['model']} {v['trim']}"),
            ("Color", v["color"]),
            ("Vehicle Identification Number (VIN)", v["vin"]),
            ("License plate", plate),
            ("Insurance company", "Capstone Mutual Insurance Company"),
            ("Policy number", p["number"]),
        ])
        _section_bar(story, styles, "Injury / EMS")
        _fields(story, styles, [
            ("Injury severity (KABCO)", "N - No apparent injury"),
            ("EMS transport", "Not transported"),
        ])
        _section_bar(story, styles, "Contributing factors")
        story.append(Paragraph(
            "Unit 1: Failed to control speed. Single vehicle departed the roadway "
            "and struck a guardrail.", styles["body"]))
    else:
        _section_bar(story, styles, "Incident information")
        offense = ("Theft of Motor Vehicle" if loss_kind == "theft"
                   else "Vehicle Fire")
        _fields(story, styles, [
            ("Offense / incident", offense),
            ("Date occurred", loss["date"]),
            ("Date reported", loss["date"]),
            ("Location of occurrence",
             f"{rng.choice(STREETS)}, {city}, {case['title_state']}"),
        ])
        _section_bar(story, styles, "Vehicle involved")
        _fields(story, styles, [
            ("Year / Make / Model", f"{v['year']} {v['make']} {v['model']} {v['trim']}"),
            ("Color", v["color"]),
            ("Vehicle Identification Number (VIN)", v["vin"]),
            ("License plate", plate),
            ("Odometer", f"{v['mileage']:,}"),
            ("Owner", f"{p['holder']} - {holder_addr}"),
        ])
        if loss_kind == "theft":
            _section_bar(story, styles, "Recovery / NCIC")
            _fields(story, styles, [
                ("Vehicle recovered", "NO - not recovered at time of report"),
                ("NCIC entry", f"Entered as stolen - NIC #{rng.choice('ABCDEFGHJKLMNPRSTUVWXYZ')}{rng.randint(100000000, 999999999)}"),
            ])

    _section_bar(story, styles, "Narrative")
    if loss_kind == "theft":
        narrative = (f"Complainant {p['holder']} states the {v['year']} {v['make']} "
                     f"{v['model']} (VIN {v['vin']}) was parked at the location above "
                     f"and was discovered missing on {loss['date']}. {loss['description']} "
                     f"Vehicle entered into NCIC as stolen. Investigation continuing.")
    elif loss_kind == "fire":
        narrative = (f"Officers responded to a vehicle fire involving a {v['year']} "
                     f"{v['make']} {v['model']} (VIN {v['vin']}) owned by {p['holder']}. "
                     f"{loss['description']} Fire department extinguished the fire; "
                     f"vehicle sustained total burn damage and was released to the owner.")
    else:
        narrative = (f"Unit 1, driven by {p['holder']}, was traveling on the roadway above "
                     f"when the driver failed to control speed and the {v['year']} {v['make']} "
                     f"{v['model']} (VIN {v['vin']}) departed the roadway and struck a "
                     f"guardrail. {loss['description']} No apparent injuries at the scene. "
                     f"Vehicle towed due to disabling damage.")
    story.append(Paragraph(narrative, styles["body"]))

    _section_bar(story, styles, "Reporting officer")
    _fields(story, styles, [
        ("Officer", officer),
        ("Badge no.", badge),
        ("Date", loss["date"]),
    ])
    _finish_doc(os.path.join(pdf_dir, "police_report.pdf"), story)


_STATE_NAMES = {"TX": "TEXAS", "GA": "GEORGIA", "CA": "CALIFORNIA"}


def _build_title(case: dict, pdf_dir: str) -> None:
    """State certificate of title."""
    v, p = case["vehicle"], case["policy"]
    rng = _case_rng(case)
    styles = _form_styles()
    city, zipc = CITIES[case["title_state"]]
    holder_addr = f"{rng.choice(STREETS)}, {city}, {case['title_state']} {zipc}"
    st = case["title_state"]
    title_no = f"{st}-T-{rng.randint(100000, 999999)}"
    lien = case["lien"]

    story = [Paragraph(f"STATE OF {_STATE_NAMES[st]}", styles["doctitle"]),
             Paragraph("Department of Motor Vehicles &nbsp;|&nbsp; Certificate of Title",
                       styles["docsub"])]
    _section_bar(story, styles, "Title record")
    _fields(story, styles, [
        ("Title number", title_no),
        ("Date issued", "2026-02-11"),
        ("Title brands", ", ".join(case["title_brands"]) or "NONE"),
    ])
    _section_bar(story, styles, "Vehicle")
    _fields(story, styles, [
        ("Vehicle Identification Number (VIN)", v["vin"]),
        ("Year / Make / Model", f"{v['year']} {v['make']} {v['model']} {v['trim']}"),
        ("Color", v["color"]),
        ("Odometer reading", f"{v['mileage']:,}"),
    ])
    _section_bar(story, styles, "Owner")
    _fields(story, styles, [
        ("Owner name", p["holder"]),
        ("Owner address", holder_addr),
    ])
    _section_bar(story, styles, "First lienholder")
    _fields(story, styles, [
        ("Lienholder",
         f"{lien['holder']} - ${lien['amount']:,}" if lien else "NONE"),
    ])
    story.append(Paragraph(
        "This certificate is issued as evidence of ownership. Any alteration "
        "voids this document. Surrender to the Department upon transfer of ownership.",
        styles["small"]))
    _sig_block(story, styles, ["Authorized signature", "Date"])
    _finish_doc(os.path.join(pdf_dir, "title.pdf"), story)


_ESTIMATE_ITEMS = [  # (operation, weight of total)
    ("Frame straightening", 0.2121),
    ("Front suspension replacement", 0.1439),
    ("Body panels + refinish", 0.3485),
    ("Airbag module replacement", 0.1162),
    ("Mechanical labor", 0.1793),
]


def _build_estimate(case: dict, pdf_dir: str) -> None:
    """Body-shop collision repair estimate with line items summing to total."""
    v, p, loss = case["vehicle"], case["policy"], case["loss"]
    rng = _case_rng(case)
    styles = _form_styles()
    city, zipc = CITIES[case["title_state"]]
    total = case["repair_estimate_total"]
    est_no = f"EST-{rng.randint(10000, 99999)}"

    story = [Paragraph("COLLISION REPAIR ESTIMATE", styles["doctitle"]),
             Paragraph("Capstone Auto Body &nbsp;|&nbsp; 4400 W Braker Ln, Austin, TX 78759"
                       " &nbsp;|&nbsp; (512) 555-0148", styles["docsub"])]
    _section_bar(story, styles, "Estimate header")
    _fields(story, styles, [
        ("Estimate no.", est_no),
        ("Estimate date", loss["date"]),
        ("Customer", p["holder"]),
        ("Insurance / Claim", f"Capstone Mutual - {case['case_id']}"),
        ("Vehicle", f"{v['year']} {v['make']} {v['model']} {v['trim']}"),
        ("Vehicle Identification Number (VIN)", v["vin"]),
        ("Mileage", f"{v['mileage']:,}"),
    ])
    _section_bar(story, styles, "Line items")
    rows = [[Paragraph("<b>#</b>", styles["k"]),
             Paragraph("<b>Operation</b>", styles["k"]),
             Paragraph("<b>Amount</b>", styles["k"])]]
    amounts, acc = [], 0
    for i, (op, w) in enumerate(_ESTIMATE_ITEMS):
        amt = total if i == len(_ESTIMATE_ITEMS) - 1 else round(w * total)
        if i == len(_ESTIMATE_ITEMS) - 1:
            amt = total - acc
        amounts.append((op, amt))
        acc += amt
    for i, (op, amt) in enumerate(amounts, 1):
        rows.append([Paragraph(str(i), styles["v"]),
                     Paragraph(op, styles["v"]),
                     Paragraph(f"${amt:,}", styles["v"])])
    rows.append([Paragraph("", styles["v"]),
                 Paragraph("<b>TOTAL ESTIMATE</b>", styles["v"]),
                 Paragraph(f"<b>${total:,}</b>", styles["v"])])
    t = Table(rows, colWidths=[0.5 * inch, 4.7 * inch, 2.3 * inch])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f5")),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#e2e8f0")),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (2, 1), (2, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t)
    story.append(Paragraph(
        "This is an estimate only; the final invoice may vary once teardown is "
        "complete. Supplement required for hidden damage.",
        styles["small"]))
    _sig_block(story, styles, ["Estimator signature", "Customer authorization"])
    _finish_doc(os.path.join(pdf_dir, "repair_estimate.pdf"), story)


def _build_pdfs(case: dict, pdf_dir: str) -> None:
    _build_fnol(case, pdf_dir)
    _build_police_report(case, pdf_dir)
    if "title" in case["documents"]:
        _build_title(case, pdf_dir)
    if "repair_estimate" in case["documents"]:
        _build_estimate(case, pdf_dir)


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

def main(seed: int = 42, out: str = "data", env_only: bool = False,
         num_cases: int = 3) -> None:
    """Generate the synthetic world.

    env_only=True writes just the environment the agent operates in — the
    statute corpus and the mock title/lien/CLUE/valuation records. No demo
    cases, no PDFs, no seeded inbox, no tally history: a fresh start where
    cases are created by the intake watcher as real claim emails arrive.
    env_only=False (default) additionally writes the demo fixtures
    (cases.json, PDFs, seeded inbox + tally) used by the eval harness and the
    scripted demo.

    num_cases: total cases to generate (first 3 are the hand-designed
    fixtures; beyond that, deterministic generated cases).
    """
    rng = random.Random(seed)
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "pdfs"), exist_ok=True)
    os.makedirs(os.path.join(out, "statutes"), exist_ok=True)

    cases = case_specs(rng, n=num_cases)

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
    p.add_argument("--num-cases", type=int, default=3,
                   help="total cases to generate (first 3 are the hand-designed fixtures)")
    a = p.parse_args()
    main(seed=a.seed, out=a.out, env_only=a.env_only, num_cases=a.num_cases)

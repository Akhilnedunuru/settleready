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
    completeness), lienholder mismatch (→ needs_info via conflict: the form
    names a different lienholder than the registry). Case IDs continue
    TL-2026-1046, ...
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
    (3, 4, ...); archetype cycles clean / undeclared-lien / missing-docs /
    lienholder-mismatch."""
    idx = i - 3
    loss_type = _GEN_LOSS_TYPES[idx % 3]
    state = _GEN_STATES[idx % 3]
    archetype = idx % 4  # 0: clean, 1: undeclared lien, 2: missing title doc,
    #                      3: lienholder mismatch (form vs registry)
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
    elif archetype == 3:  # conflict demo: form names a DIFFERENT lienholder
        # than the registry (e.g. a refinance the registry has not caught up
        # with) — both values are preserved for analyst verification.
        lien = {"holder": rng.choice(_GEN_LIENHOLDERS),
                "amount": rng.randint(3000, 9000)}
        declared_lien = True
        others = [h for h in _GEN_LIENHOLDERS if h != lien["holder"]]
        case_extra["form_lien_holder"] = rng.choice(others)
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
# Realistic claim documents modeled on real-world forms (ACORD 2 Automobile
# Loss Notice, Texas CR-3 crash report, state certificate of title, body-shop
# repair estimate): field grids with boxed inputs, checkboxes, and form flow.
# Layouts are original; all data is synthetic.

_BANNER_PDF = BANNER.replace("\u2014", "-")
_FORM_W = 7.5


def _form_styles() -> dict:
    return {
        "doctitle": ParagraphStyle("doctitle", fontName="Helvetica-Bold",
                                   fontSize=15, leading=18, spaceAfter=1),
        "docsub": ParagraphStyle("docsub", fontSize=9, leading=11, spaceAfter=4,
                                 textColor=colors.HexColor("#555555")),
        "flabel": ParagraphStyle("flabel", fontName="Helvetica-Bold", fontSize=7,
                                 leading=9, textColor=colors.HexColor("#333333")),
        "fval": ParagraphStyle("fval", fontSize=9, leading=12),
        "sechead": ParagraphStyle("sechead", fontName="Helvetica-Bold", fontSize=9,
                                  leading=11, spaceAfter=1,
                                  textColor=colors.HexColor("#1a1a1a")),
        "body": ParagraphStyle("body", fontSize=9, leading=13, spaceAfter=6),
        "small": ParagraphStyle("small", fontSize=7.5, leading=10, spaceAfter=4,
                                textColor=colors.HexColor("#555555")),
        "cb": ParagraphStyle("cb", fontName="Helvetica-Bold", fontSize=10,
                             leading=12, alignment=1),
        "cblab": ParagraphStyle("cblab", fontSize=8, leading=11),
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


def _field_box(styles: dict, label: str, value, width_in: float):
    """One form field: small label above a bordered input box."""
    txt = str(value) if str(value) else "&nbsp;"
    inner = Table([
        [Paragraph(label.upper(), styles["flabel"])],
        [Paragraph(txt, styles["fval"])],
    ], colWidths=[width_in * inch])
    inner.setStyle(TableStyle([
        ("BOX", (0, 1), (0, 1), 0.7, colors.HexColor("#222222")),
        ("TOPPADDING", (0, 1), (0, 1), 5),
        ("BOTTOMPADDING", (0, 1), (0, 1), 5),
        ("LEFTPADDING", (0, 1), (0, 1), 6),
        ("BOTTOMPADDING", (0, 0), (0, 0), 2),
        ("LEFTPADDING", (0, 0), (0, 0), 1),
    ]))
    return inner


def _checkbox(styles: dict, checked: bool):
    t = Table([[Paragraph("<b>X</b>" if checked else "&nbsp;", styles["cb"])]],
              colWidths=[0.24 * inch], rowHeights=[0.24 * inch])
    t.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.9, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))
    return t


def _yn_field(styles: dict, label: str, is_yes: bool, width_in: float):
    """Yes/No field rendered as real checkboxes, label BEFORE its box.

    The [X] mark therefore text-extracts immediately AFTER the option it
    selects ("YES NO X" == NO selected) instead of floating ambiguously
    between the two labels ("YES X NO").
    """
    opts = Table([
        [Paragraph("YES", styles["cblab"]), _checkbox(styles, is_yes),
         Paragraph("NO", styles["cblab"]), _checkbox(styles, not is_yes)],
    ], colWidths=[0.45 * inch, 0.24 * inch, 0.45 * inch, 0.24 * inch])
    opts.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 2)]))
    inner = Table([
        [Paragraph(label.upper(), styles["flabel"])],
        [opts],
    ], colWidths=[width_in * inch])
    inner.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 1),
                               ("BOTTOMPADDING", (0, 0), (0, 0), 2)]))
    return inner


def _form_grid(story: list, styles: dict, *rows) -> None:
    """Lay out form fields in rows. Each row is a list of
    (label, value, width_inches) tuples; widths in a row must sum to 7.5.
    A bool value renders as YES/NO checkboxes."""
    for row in rows:
        cells, widths = [], []
        for label, value, w in row:
            widths.append(w * inch)
            if isinstance(value, bool):
                cells.append(_yn_field(styles, label, value, w))
            else:
                cells.append(_field_box(styles, label, value, w))
        t = Table([cells], colWidths=widths)
        t.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(t)


def _form_section(story: list, styles: dict, text: str) -> None:
    story.append(Spacer(1, 0.14 * inch))
    story.append(Paragraph(text.upper(), styles["sechead"]))
    rule = Table([[""]], colWidths=[_FORM_W * inch])
    rule.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.8,
                               colors.HexColor("#1a1a1a"))]))
    story.append(rule)
    story.append(Spacer(1, 0.04 * inch))


def _doc_header(story: list, styles: dict, title: str, sub: str,
                right_label: str, right_value: str) -> None:
    left = [Paragraph(title, styles["doctitle"]),
            Paragraph(sub, styles["docsub"])]
    right = _field_box(styles, right_label, right_value, 2.2)
    t = Table([[left, right]], colWidths=[5.3 * inch, 2.2 * inch])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LEFTPADDING", (1, 0), (1, 0), 8)]))
    story.append(t)
    story.append(Spacer(1, 0.08 * inch))

_OFFICERS = ["J. Martinez", "R. Thompson", "K. Okafor", "D. Nguyen", "S. Patel"]


def _case_rng(case: dict) -> random.Random:
    # deterministic cosmetic extras (plates, DL nos, badge nos) per case
    return random.Random(1000 + int(case["case_id"].rsplit("-", 1)[-1]))


def _plate(rng: random.Random, state: str) -> str:
    return f"{state} {''.join(rng.choice('ABCDEFGHJKLMNPRSTUVWXYZ') for _ in range(3))}-{rng.randint(1000, 9999)}"


def _build_fnol(case: dict, pdf_dir: str) -> None:
    """First Notice of Loss - Automobile (ACORD 2 Automobile Loss Notice style)."""
    v, p, loss = case["vehicle"], case["policy"], case["loss"]
    rng = _case_rng(case)
    styles = _form_styles()
    st = case["title_state"]
    city, zipc = CITIES[st]
    holder_addr = f"{rng.choice(STREETS)}, {city}, {st} {zipc}"
    phone = f"(555) 01{rng.randint(0, 9)}-{rng.randint(1000, 9999):04d}"
    plate = _plate(rng, st)
    loss_time = f"{rng.randint(1, 12)}:{rng.randint(10, 59):02d}"
    ampm = rng.choice(["AM", "PM"])
    driver = ("Unknown - vehicle stolen" if case["loss_type"] == "theft"
              else f"{p['holder']}, {holder_addr}")

    story = []
    _doc_header(story, styles, "AUTOMOBILE LOSS NOTICE",
                "Capstone Mutual Insurance Company - Auto Claims",
                "Claim no.", case["case_id"])

    _form_section(story, styles, "Agency / Company")
    _form_grid(story, styles,
               [("Agency", "Capstone Mutual Agency #0417", 3.2),
                ("Phone (A/C, No)", "(512) 555-0148", 2.1),
                ("NAIC code", "12345", 2.2)])

    _form_section(story, styles, "Policy")
    _form_grid(story, styles,
               [("Policy number", p["number"], 2.5),
                ("Policy type", "Personal Auto", 2.0),
                ("Effective date", "01/01/2026", 1.5),
                ("Expiration date", "12/31/2026", 1.5)])

    _form_section(story, styles, "Insured")
    _form_grid(story, styles,
               [("Insured name", p["holder"], 4.0),
                ("Residence phone", phone, 3.5)],
               [("Insured address", holder_addr, 7.5)])

    _form_section(story, styles, "Loss")
    _form_grid(story, styles,
               [("Date of loss", loss["date"], 2.0),
                ("Time of loss", loss_time, 1.7),
                ("AM / PM", ampm, 1.3),
                ("Loss type", case["loss_type"].upper(), 2.5)],
               [("Location of loss", f"{city}, {st}", 7.5)],
               [("Authority contacted", loss["agency"], 4.5),
                ("Report #", loss["police_report_no"], 3.0)],
               [("Description of loss", loss["description"], 7.5)])

    _form_section(story, styles, "Policy information")
    _form_grid(story, styles,
               [("Bodily injury per person", "$100,000", 1.875),
                ("Bodily injury per accident", "$300,000", 1.875),
                ("Property damage", "$100,000", 1.875),
                ("Deductible", "$500", 1.875)])

    _form_section(story, styles, "Insured vehicle")
    _form_grid(story, styles,
               [("Year", str(v["year"]), 1.0),
                ("Make", v["make"], 1.8),
                ("Model", f"{v['model']} {v['trim']}", 2.2),
                ("Plate", plate, 1.5),
                ("State", st, 1.0)],
               [("V.I.N.", v["vin"], 5.0),
                ("Mileage", f"{v['mileage']:,}", 2.5)],
               [("Owner name & address", f"{p['holder']} - {holder_addr}", 5.0),
                ("Lienholder?", bool(case["declared_lien"]), 2.5)],
               [("Driver name & address", driver, 7.5)])
    if case["declared_lien"]:
        # The name the claimant wrote. Normally the registry holder; mismatch
        # fixtures carry form_lien_holder (a different name) on purpose.
        form_holder = (case.get("form_lien_holder")
                       or (case["lien"] or {}).get("holder", ""))
        _form_grid(story, styles,
                   [("Lienholder name", form_holder, 7.5)])

    _form_section(story, styles, "Remarks")
    _form_grid(story, styles,
               [("Remarks",
                 "Total loss suspected. Assigned to Auto Physical Damage unit.", 7.5)])
    story.append(Paragraph(
        "Fraud notice: Any person who knowingly presents a false or fraudulent claim "
        "for payment of a loss is subject to criminal and civil penalties.",
        styles["small"]))

    _form_section(story, styles, "Signatures")
    _form_grid(story, styles,
               [("Reported by", p["holder"], 2.5),
                ("Reported to", "Auto Claims Dept", 2.5),
                ("Signature of insured", "", 2.5)])
    _finish_doc(os.path.join(pdf_dir, "claim_form.pdf"), story)

def _build_police_report(case: dict, pdf_dir: str) -> None:
    """Police report: CR-3 style crash report for collisions, incident/offense
    report for theft and fire."""
    v, p, loss = case["vehicle"], case["policy"], case["loss"]
    rng = _case_rng(case)
    styles = _form_styles()
    st = case["title_state"]
    city, zipc = CITIES[st]
    counties = {"TX": "Travis", "GA": "Fulton", "CA": "Fresno"}
    holder_addr = f"{rng.choice(STREETS)}, {city}, {st} {zipc}"
    plate = _plate(rng, st)
    officer = rng.choice(_OFFICERS)
    badge = str(rng.randint(1000, 9999))
    kind = case["loss_type"]
    when = f"{rng.randint(1, 12)}:{rng.randint(10, 59):02d} {rng.choice(['AM', 'PM'])}"
    ymm = f"{v['year']} {v['make']} {v['model']} {v['trim']}"

    story = []
    if kind == "collision":
        _doc_header(story, styles, "PEACE OFFICER'S CRASH REPORT",
                    f"{loss['agency']} - Traffic Division",
                    "Report no.", loss["police_report_no"])
        _form_section(story, styles, "Crash identification")
        _form_grid(story, styles,
                   [("Crash date", loss["date"], 1.8),
                    ("Crash time", when, 1.7),
                    ("City", city, 2.0),
                    ("County", counties[st], 2.0)],
                   [("Location", f"{rng.choice(STREETS)}, {city}, {st}", 7.5)],
                   [("Weather", "Clear", 1.875),
                    ("Road surface", "Dry", 1.875),
                    ("Light condition", "Daylight", 1.875),
                    ("Traffic control", "None", 1.875)])
        _form_section(story, styles, "Unit 1 - Driver")
        _form_grid(story, styles,
                   [("Driver name", p["holder"], 3.2),
                    ("Driver license no.",
                     f"{st}{rng.randint(10000000, 99999999)}", 2.8),
                    ("DL state", st, 1.5)],
                   [("Driver address", holder_addr, 7.5)])
        _form_section(story, styles, "Unit 1 - Vehicle")
        _form_grid(story, styles,
                   [("Year", str(v["year"]), 1.0),
                    ("Make", v["make"], 1.6),
                    ("Model", f"{v['model']} {v['trim']}", 2.0),
                    ("Color", v["color"], 1.4),
                    ("Plate", plate, 1.5)],
                   [("V.I.N.", v["vin"], 5.0),
                    ("Plate state", st, 2.5)],
                   [("Insurance company", "Capstone Mutual Insurance Company", 4.5),
                    ("Policy #", p["number"], 3.0)])
        _form_section(story, styles, "Injury / EMS")
        _form_grid(story, styles,
                   [("Injured?", False, 2.5),
                    ("EMS transport", "Not transported", 2.5),
                    ("Severity (KABCO)", "N - No apparent injury", 2.5)])
        _form_section(story, styles, "Contributing factors")
        _form_grid(story, styles,
                   [("Unit 1 contributing factor",
                     "Failed to control speed", 7.5)])
        narrative = (f"Unit 1, driven by {p['holder']}, was traveling on the roadway "
                     f"above when the driver failed to control speed and the {ymm} "
                     f"(VIN {v['vin']}) departed the roadway and struck a guardrail. "
                     f"{loss['description']} No apparent injuries at the scene. "
                     f"Vehicle towed due to disabling damage.")
    else:
        offense = ("Theft of Motor Vehicle" if kind == "theft" else "Vehicle Fire")
        _doc_header(story, styles, "INCIDENT / OFFENSE REPORT",
                    f"{loss['agency']} - {offense}",
                    "Report no.", loss["police_report_no"])
        _form_section(story, styles, "Incident information")
        _form_grid(story, styles,
                   [("Offense", offense, 3.5),
                    ("Date occurred", loss["date"], 2.0),
                    ("Date reported", loss["date"], 2.0)],
                   [("Location of occurrence",
                     f"{rng.choice(STREETS)}, {city}, {st}", 7.5)])
        _form_section(story, styles, "Vehicle involved")
        _form_grid(story, styles,
                   [("Year", str(v["year"]), 1.0),
                    ("Make", v["make"], 1.6),
                    ("Model", f"{v['model']} {v['trim']}", 2.0),
                    ("Color", v["color"], 1.4),
                    ("Plate", plate, 1.5)],
                   [("V.I.N.", v["vin"], 5.0),
                    ("Odometer", f"{v['mileage']:,}", 2.5)],
                   [("Owner name & address",
                     f"{p['holder']} - {holder_addr}", 7.5)])
        if kind == "theft":
            _form_section(story, styles, "Recovery")
            nic = (rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ")
                   + str(rng.randint(100000000, 999999999)))
            _form_grid(story, styles,
                       [("Vehicle recovered?", False, 2.5),
                        ("NCIC entry", f"Entered as stolen - NIC #{nic}", 5.0)])
            narrative = (f"Complainant {p['holder']} states the {ymm} (VIN {v['vin']}) "
                         f"was parked at the location above and was discovered missing "
                         f"on {loss['date']}. {loss['description']} Vehicle entered "
                         f"into NCIC as stolen. Investigation continuing.")
        else:
            narrative = (f"Officers responded to a vehicle fire involving a {ymm} "
                         f"(VIN {v['vin']}) owned by {p['holder']}. {loss['description']} "
                         f"Fire department extinguished the fire; vehicle sustained "
                         f"total burn damage and was released to the owner.")

    _form_section(story, styles, "Narrative")
    _form_grid(story, styles, [("Narrative", narrative, 7.5)])
    _form_section(story, styles, "Reporting officer")
    _form_grid(story, styles,
               [("Officer name", officer, 3.0),
                ("Badge no.", badge, 2.0),
                ("Date", loss["date"], 2.5)])
    _finish_doc(os.path.join(pdf_dir, "police_report.pdf"), story)

_STATE_NAMES = {"TX": "TEXAS", "GA": "GEORGIA", "CA": "CALIFORNIA"}


def _build_title(case: dict, pdf_dir: str) -> None:
    """State certificate of title."""
    v, p = case["vehicle"], case["policy"]
    rng = _case_rng(case)
    styles = _form_styles()
    st = case["title_state"]
    city, zipc = CITIES[st]
    holder_addr = f"{rng.choice(STREETS)}, {city}, {st} {zipc}"
    title_no = f"{st}-T-{rng.randint(100000, 999999)}"
    lien = case["lien"]

    story = []
    _doc_header(story, styles, "CERTIFICATE OF TITLE",
                f"State of {_STATE_NAMES[st].title()} - Department of Motor Vehicles",
                "Title no.", title_no)

    _form_section(story, styles, "Title record")
    _form_grid(story, styles,
               [("Date issued", "02/11/2026", 2.5),
                ("Odometer reading", f"{v['mileage']:,}", 2.5),
                ("Title brands",
                 ", ".join(case["title_brands"]) or "NONE", 2.5)],
               [("Vehicle Identification Number (V.I.N.)", v["vin"], 7.5)])

    _form_section(story, styles, "Vehicle")
    _form_grid(story, styles,
               [("Year", str(v["year"]), 1.2),
                ("Make", v["make"], 1.8),
                ("Model", f"{v['model']} {v['trim']}", 2.3),
                ("Color", v["color"], 2.2)])

    _form_section(story, styles, "Owner")
    _form_grid(story, styles,
               [("Owner name", p["holder"], 3.5),
                ("Owner address", holder_addr, 4.0)])

    _form_section(story, styles, "Lienholder")
    _form_grid(story, styles,
               [("First lienholder",
                 f"{lien['holder']} - ${lien['amount']:,}" if lien else "NONE", 7.5)])

    story.append(Paragraph(
        "This certificate is issued as evidence of ownership. Any alteration voids "
        "this document. Surrender to the Department upon transfer of ownership.",
        styles["small"]))
    _form_section(story, styles, "Signatures")
    _form_grid(story, styles,
               [("Authorized signature", "", 3.75),
                ("Date", "", 3.75)])
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
    total = case["repair_estimate_total"]
    est_no = f"EST-{rng.randint(10000, 99999)}"

    story = []
    _doc_header(story, styles, "COLLISION REPAIR ESTIMATE",
                "Capstone Auto Body - 4400 W Braker Ln, Austin, TX 78759"
                " - (512) 555-0148",
                "Estimate no.", est_no)

    _form_section(story, styles, "Estimate header")
    _form_grid(story, styles,
               [("Estimate date", loss["date"], 2.5),
                ("Customer", p["holder"], 5.0)],
               [("Insurance / Claim",
                 f"Capstone Mutual - {case['case_id']}", 7.5)],
               [("Vehicle",
                 f"{v['year']} {v['make']} {v['model']} {v['trim']}", 7.5)],
               [("V.I.N.", v["vin"], 5.0),
                ("Mileage", f"{v['mileage']:,}", 2.5)])

    _form_section(story, styles, "Line items")
    rows = [[Paragraph("<b>#</b>", styles["flabel"]),
             Paragraph("<b>OPERATION</b>", styles["flabel"]),
             Paragraph("<b>AMOUNT</b>", styles["flabel"])]]
    amounts, acc = [], 0
    for i, (op, w) in enumerate(_ESTIMATE_ITEMS):
        if i == len(_ESTIMATE_ITEMS) - 1:
            amt = total - acc
        else:
            amt = round(w * total)
        amounts.append((op, amt))
        acc += amt
    for i, (op, amt) in enumerate(amounts, 1):
        rows.append([Paragraph(str(i), styles["fval"]),
                     Paragraph(op, styles["fval"]),
                     Paragraph(f"${amt:,}", styles["fval"])])
    rows.append([Paragraph("", styles["fval"]),
                 Paragraph("<b>TOTAL ESTIMATE</b>", styles["fval"]),
                 Paragraph(f"<b>${total:,}</b>", styles["fval"])])
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
    _form_section(story, styles, "Authorization")
    _form_grid(story, styles,
               [("Estimator signature", "", 3.75),
                ("Customer authorization", "", 3.75)])
    _finish_doc(os.path.join(pdf_dir, "repair_estimate.pdf"), story)


def _build_pdfs(case: dict, pdf_dir: str) -> None:
    _build_fnol(case, pdf_dir)
    _build_police_report(case, pdf_dir)
    if "title" in case["documents"]:
        _build_title(case, pdf_dir)
    if "repair_estimate" in case["documents"]:
        _build_estimate(case, pdf_dir)


_DOC_BUILDERS = {
    "claim_form": _build_fnol,
    "police_report": _build_police_report,
    "title": _build_title,
    "repair_estimate": _build_estimate,
}


def gen_doc(out: str, case_id: str, doc_type: str) -> str:
    """Generate a single missing document for a case (demo follow-ups).

    Reads the case spec from <out>/cases.json and writes the PDF into
    <out>/pdfs/<case_id>/. Used when the customer replies with a document
    the analyst asked for.
    """
    import json as _json
    import os as _os
    if doc_type not in _DOC_BUILDERS:
        raise ValueError(f"doc_type must be one of {sorted(_DOC_BUILDERS)}")
    with open(_os.path.join(out, "cases.json")) as f:
        cases = _json.load(f)
    case = next((c for c in cases if c["case_id"] == case_id), None)
    if case is None:
        raise ValueError(f"unknown case {case_id} in {out}/cases.json")
    pdf_dir = _os.path.join(out, "pdfs", case_id)
    _os.makedirs(pdf_dir, exist_ok=True)
    _DOC_BUILDERS[doc_type](case, pdf_dir)
    path = _os.path.join(pdf_dir, f"{doc_type}.pdf")
    print(f"Generated {path}")
    return path


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
         num_cases: int = 3, tally_seed: bool = True) -> None:
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

    # Seeded tally history: 2x GA UNDECLARED_LIEN (one more promotes the check).
    # Skip with tally_seed=False for a truly empty episodic memory while testing.
    if tally_seed:
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
    p.add_argument("--gen-doc", nargs=2, metavar=("CASE_ID", "DOC_TYPE"),
                   help="generate one missing document for a case "
                        "(claim_form|police_report|title|repair_estimate)")
    p.add_argument("--no-tally-seed", action="store_true",
                   help="leave the return-tally empty (no pre-seeded history)")
    a = p.parse_args()
    if a.gen_doc:
        gen_doc(a.out, a.gen_doc[0], a.gen_doc[1])
    else:
        main(seed=a.seed, out=a.out, env_only=a.env_only, num_cases=a.num_cases,
             tally_seed=not a.no_tally_seed)

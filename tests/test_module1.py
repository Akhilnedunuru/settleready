

def test_checkbox_mark_follows_selected_option(tmp_path):
    """Regression: YES/NO checkbox state must be unambiguous in extracted text.

    The [X] mark must text-extract immediately AFTER the option it selects
    ('YES NO X' == NO), never floating between labels ('YES X NO').
    """
    import random, pdfplumber
    from settleready.data.seed import case_specs, _build_pdfs
    cs = case_specs(random.Random(42), n=15)
    for cid, want_after in (("TL-2026-1047", "NO"), ("TL-2026-1045", "YES")):
        c = [x for x in cs if x["case_id"] == cid][0]
        d = tmp_path / cid
        d.mkdir()
        _build_pdfs(c, str(d))
        with pdfplumber.open(str(d / "claim_form.pdf")) as pdf:
            txt = pdf.pages[1].extract_text()
        line = next(l for l in txt.split("\n") if "YES" in l and "NO" in l)
        # the option immediately preceding the X is the selected one
        before_x = line[: line.index("X")]
        selected = "YES" if before_x.rfind("YES") > before_x.rfind("NO") else "NO"
        assert selected == want_after, f"{cid}: X marks {selected}: {line!r}"

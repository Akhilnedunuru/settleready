"""PDF parser tests — hermetic (generates its own fixture PDF with reportlab)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from settleready.tools import pdf_parser


def _make_pdf(path):
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import Paragraph, SimpleDocTemplate
    from reportlab.lib.styles import ParagraphStyle
    s = ParagraphStyle("s", fontSize=11, leading=14)
    SimpleDocTemplate(path, pagesize=letter).build([
        Paragraph("VIN 1HGCV1F34KA000001", s),
        Paragraph("Policy number TX-A-4829137", s),
    ])


def test_parse_pdf_text(tmp_path):
    pdf = str(tmp_path / "t.pdf")
    _make_pdf(pdf)
    pages = pdf_parser.parse_document(pdf)
    assert len(pages) == 1
    assert "1HGCV1F34KA000001" in pages[0]["text"]
    assert pages[0]["needs_ocr"] is False


def test_unsupported_type(tmp_path):
    try:
        pdf_parser.parse_document(str(tmp_path / "x.docx"))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for .docx")

"""Document parser: PDF / image -> structured text (module 2).

Strategy per document:
  1. pdfplumber (layout-aware, keeps tables) — primary for PDFs
  2. pypdf fallback — if pdfplumber yields no text
  3. Tesseract OCR — for image files; PDF pages with no extractable text are
     flagged ``needs_ocr=True`` rather than silently returning empty text

Output: [{"page": int, "text": str, "tables": [...], "needs_ocr": bool}]

CLI:  PYTHONPATH=src python -m settleready.tools.pdf_parser <file.pdf>
"""
from __future__ import annotations

import os


def parse_document(path: str) -> list[dict]:
    """Parse a PDF or image into per-page text blocks."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return _parse_pdf(path)
    if ext in (".png", ".jpg", ".jpeg", ".tiff", ".bmp"):
        return _parse_image(path)
    raise ValueError(f"Unsupported file type: {ext!r}")


def _parse_pdf(path: str) -> list[dict]:
    pages = _pdfplumber_pages(path)
    if not pages or all(not p["text"].strip() for p in pages):
        pages = _pypdf_pages(path)
    for p in pages:
        p.setdefault("needs_ocr", not p["text"].strip())
    return pages


def _pdfplumber_pages(path: str) -> list[dict]:
    try:
        import pdfplumber
    except ImportError:
        return []
    out = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            try:
                tables = page.extract_tables() or []
            except Exception:
                tables = []
            out.append({"page": i, "text": text, "tables": tables})
    return out


def _pypdf_pages(path: str) -> list[dict]:
    from pypdf import PdfReader
    reader = PdfReader(path)
    out = []
    for i, pg in enumerate(reader.pages, start=1):
        try:
            text = pg.extract_text() or ""
        except Exception:
            text = ""
        out.append({"page": i, "text": text, "tables": []})
    return out


def _parse_image(path: str) -> list[dict]:
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return [{"page": 1, "text": "", "tables": [],
                 "needs_ocr": True, "ocr_error": "pytesseract/Pillow not installed"}]
    try:
        from PIL import Image
        text = pytesseract.image_to_string(Image.open(path))
        return [{"page": 1, "text": text, "tables": [], "needs_ocr": False}]
    except Exception as e:  # tesseract binary missing, corrupt image, ...
        return [{"page": 1, "text": "", "tables": [],
                 "needs_ocr": True, "ocr_error": str(e)}]


if __name__ == "__main__":
    import sys
    for page in parse_document(sys.argv[1]):
        print(f'--- page {page["page"]} (needs_ocr={page.get("needs_ocr")}) ---')
        print(page["text"][:600])

"""Document parser: PDF / image -> structured text (module 2/3).

- Text PDFs: pdfplumber (layout-aware, tables) with pypdf fallback.
- Scanned images: Tesseract OCR fallback (needs `brew install tesseract`).
- Output: list of {page, text, blocks} preserving source spans for citations.

CLI for RAG indexing:  python -m settleready.tools.pdf_parser extract <file>
"""
from __future__ import annotations


def parse_document(path: str) -> list[dict]:
    """TODO(module 2): return [{'page': int, 'text': str, 'tables': [...]}]."""
    raise NotImplementedError


if __name__ == "__main__":
    import sys
    print(parse_document(sys.argv[1]))

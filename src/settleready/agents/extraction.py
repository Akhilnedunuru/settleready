"""Extraction agent (module 2/3).

Parses attachments and extracts structured fields (VIN, mileage, title state,
lienholder, loss facts) — each with a confidence score and source span.

Failure modes it must handle: missing fields, low OCR confidence (< 0.8 ->
flag, don't trust), conflicting values across documents.
"""
from __future__ import annotations


def extract_fields(parsed_docs: list[dict]) -> dict:
    """TODO(module 2): ReAct loop over Doc Parser output -> fields dict."""
    raise NotImplementedError

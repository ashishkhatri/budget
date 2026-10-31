"""File parsers. ``parse_file`` picks CSV or PDF by extension / content."""
from __future__ import annotations

from .common import Candidate, ParseError, ParseResult
from .csv_parser import parse_csv
from .pdf_parser import parse_pdf

__all__ = ["Candidate", "ParseError", "ParseResult", "parse_csv", "parse_pdf", "parse_file"]


def parse_file(data: bytes, filename: str) -> ParseResult:
    if filename.lower().endswith(".pdf") or data[:5] == b"%PDF-":
        return parse_pdf(data, filename)
    return parse_csv(data, filename)

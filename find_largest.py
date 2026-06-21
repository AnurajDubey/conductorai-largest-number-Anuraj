#!/usr/bin/env python3
"""Find the largest number in a budget PDF — both *raw* and *scale-adjusted*.

The program reads a PDF (default: the bundled FY25 Air Force Working Capital
Fund budget) and reports TWO independent maxima:

  1. RAW max          — the greatest numerical value exactly as printed.
  2. SCALE-ADJUSTED   — the greatest value after applying the document's own
                        natural-language scale guidance (a table headed
                        "(Dollars in Millions)" means 3.15 -> 3,150,000).

These two winners almost always come from different places, so they are
tracked separately.

Guiding principle
-----------------
The output is a single maximum, so ONE bad candidate corrupts the whole
result. Every heuristic here optimises for *precision on the extreme value*
rather than recall on the bulk: it matters far more that the winning number
is real than that every cell in the document is parsed perfectly.

Five stages (see the sections below):
  1. Extract text per page (thin pdfplumber layer, kept swappable).
  2. Detect scale declarations per page  -> a per-page `Scale`.
  3. Extract number candidates and parse them to floats.
  4. Resolve each candidate's multiplier and filter out non-values.
  5. Track the two maxima together with provenance.

Run:  python find_largest.py [path/to.pdf] [--include-identifiers]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from typing import Iterator, List, Optional

import pdfplumber

# Bundled document is the default so the program runs with no arguments, yet a
# path can be supplied to point it at any other PDF.  No absolute paths, no
# network access — everything is resolved relative to this file.
DEFAULT_PDF = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data",
    "FY25_Air_Force_Working_Capital_Fund.pdf",
)


# ===========================================================================
# Stage 1 — Extraction layer (thin + swappable)
# ===========================================================================
# Only this function talks to pdfplumber.  The rest of the program works on a
# list of plain strings, so the extraction library could be swapped without
# touching the parsing/scaling logic.  Word-level positions are available from
# pdfplumber if ever needed, but for this document line text is sufficient:
# the scale guidance lives in page header text, not in column geometry, so no
# table reconstruction is required.

def extract_pages(pdf_path: str) -> List[str]:
    """Return the text of each page as a string (index 0 == page 1)."""
    pages: List[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return pages


# ===========================================================================
# Stage 2 — Per-page scale declarations ("banners")
# ===========================================================================
# In this document the scale is declared in block/page HEADER text, e.g.
# "(Dollars in Millions)", and governs the whole page.  So page-scoped
# detection is enough (no per-column table geometry needed).
#
# A banner carries a MULTIPLIER and a UNIT.  The unit matters: a banner only
# scales numbers *of its own unit*.  A "(Dollars in Millions)" banner scales
# dollar figures but must NOT scale a headcount or a receipts count that
# happens to sit in the same table — doing so invents trillions that the
# document never states.

@dataclass(frozen=True)
class Scale:
    multiplier: int
    unit: Optional[str]  # "dollars", "hours", or None
    label: str           # the banner text we matched, kept for provenance


NO_SCALE = Scale(1, None, "none")

# Variants are matched case-insensitively with tolerant interior spacing.
# Observed in the document: "(Dollars in Millions)", "($ Millions)", "($M)",
# "(millions)", "($ IN MILLIONS)", "(Dollars in Thousands)" and a single
# non-dollar "(Hours in Thousands)".  Deliberately no "billion" form — the
# document contains no billions tables (only a "$9.6 billion" prose mention,
# which is handled as an inline magnitude, not a banner).
_MILLIONS = re.compile(
    r"\(\s*(?:dollars?\s+in\s+millions?|\$\s*(?:in\s+)?millions?|millions?|\$\s*m)\s*\)",
    re.I,
)
_THOUSANDS = re.compile(
    r"\(\s*(?:dollars?\s+in\s+thousands?|\$\s*(?:in\s+)?thousands?)\s*\)",
    re.I,
)
_HOURS_THOUSANDS = re.compile(r"\(\s*hours?\s+in\s+thousands?\s*\)", re.I)


def detect_scale(page_text: str) -> Scale:
    """Detect the governing scale banner for a page.

    Order matters: the non-dollar "(Hours in Thousands)" banner is checked
    before the generic thousands/millions dollar banners so that hours are
    never mistaken for a dollar scale.
    """
    if _HOURS_THOUSANDS.search(page_text):
        return Scale(1_000, "hours", "(Hours in Thousands)")
    if _MILLIONS.search(page_text):
        return Scale(1_000_000, "dollars", "(Dollars in Millions)")
    if _THOUSANDS.search(page_text):
        return Scale(1_000, "dollars", "(Dollars in Thousands)")
    return NO_SCALE


# ===========================================================================
# Stage 3 — Number candidates
# ===========================================================================
# A candidate is one numeric token plus everything we need to decide its
# multiplier later: the page, its line label, whether it is identifier-shaped,
# whether its row is a non-dollar quantity, whether it sits in prose, and any
# magnitude it carries inline (a "M"/"billion" suffix).

@dataclass
class Candidate:
    face: float            # the numeral exactly as printed (the RAW value)
    token: str             # the matched text, e.g. "$6,000,000" or "30,704.1"
    page: int              # 1-based page number
    line: str              # the full line the token came from
    inline_mult: Optional[int]   # magnitude attached to the number itself
    inline_label: str            # e.g. "M", "billion" (for provenance)
    is_identifier: bool          # stock number / PE code / phone / digit-blob
    is_count_row: bool           # row label denotes a count/quantity, not $
    in_prose: bool               # number embedded in a running sentence


# --- number tokeniser -------------------------------------------------------
# A well-formed number, anchored so it is NOT glued to letters or to another
# number.  The leading look-behind is the key robustness guard: this document
# contains pages with doubled ("fake bold") text where two copies overlap and
# the text layer interleaves a label with its value, e.g.
#       "Total Other Fun1d,7 A7c4t.8iv3i1ty Groups"
# Requiring that a number is NOT preceded by a letter/digit/comma/dot discards
# those interleaved fragments (the "4" in "A7c4t" is preceded by "c" -> skip),
# so they can never become phantom candidates.
_NUMBER = re.compile(
    r"(?<![A-Za-z0-9.,])"               # left boundary: not glued to a word/number
    r"[\$(]{0,2}"                       # optional leading "$", "(", or "$("
    r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?"  # comma-grouped: 1,754,801 or 30,704.1
    r"|\d+(?:\.\d+)?)"                  # or a plain run: 234, 112.750, 0708055
    r"\)?"                              # optional trailing ")"
)

# Magnitudes a number can carry *inline* (these win over any page banner —
# the magnitude is already baked in, so applying the banner too would double
# count, e.g. "$234M" must stay 234,000,000, never 234,000,000 x 1,000,000).
_SUFFIX_MULT = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000, "T": 1_000_000_000_000}
_WORD_MULT = {"thousand": 1_000, "million": 1_000_000,
              "billion": 1_000_000_000, "trillion": 1_000_000_000_000}
# Attached suffix must be UPPERCASE (as written: "$234M", "$78.0M") and not
# followed by another letter.  Upper-case-only is deliberate: it stops the
# stray lower-case letter of an adjacent word (the "t" in "...4t.8...") from
# being read as a Trillion suffix.
_SUFFIX_RE = re.compile(r"([KMBT])(?![A-Za-z])")
_WORD_RE = re.compile(r"\s?(thousand|million|billion|trillion)s?\b", re.I)


def _clean_token(token: str) -> str:
    """Tidy a token for display by dropping an unbalanced outer parenthesis.

    The tokeniser may grab a sentence's parenthesis (e.g. "$6,000,000)" where
    the "(" belongs to "(costing ...)").  A balanced "(302.3)" — an accounting
    negative — is left untouched.
    """
    if token.endswith(")") and "(" not in token:
        token = token[:-1]
    if token.startswith("(") and ")" not in token:
        token = token[1:]
    return token.strip()


def _parse_face(token: str) -> Optional[float]:
    """Parse a token to its printed numeric value.

    Parentheses are read as the accounting convention for a negative number
    ("(302.3)" -> -302.3).  For a maximum this is the safe reading: it can
    only keep a parenthesised value from winning, never invent a large one.
    """
    negative = "(" in token and ")" in token
    digits = re.sub(r"[^\d.]", "", token)
    if digits in ("", ".") or digits.count(".") > 1:
        return None
    value = float(digits)
    return -value if negative else value


def _classify_identifier(digits: str, token: str, glued_to_letters: bool) -> bool:
    """True if the token looks like a label-in-digits rather than a value.

    Identifier tells (per the brief): letters attached (PE code "0708055F"),
    a leading zero, or a long ungrouped digit-run with no comma grouping.
    Well-formed values are comma-grouped and/or "$"-prefixed.
    """
    if glued_to_letters:
        return True
    # Leading-zero code (e.g. "0708055"), but not a fractional value like "0.5".
    if "." not in token and len(digits) > 1 and digits.startswith("0"):
        return True
    # Long un-grouped integer run (no comma grouping, no decimal).
    if "," not in token and "." not in token and len(digits) >= 5:
        return True
    return False


# Row labels that denote a COUNT / QUANTITY rather than a dollar amount.  Under
# a dollar banner these rows must NOT be scaled (scaling "Number of Receipts
# 1,754,801" by a millions banner invents a 1.75-trillion phantom that wrongly
# wins).  The list targets count/quantity row labels actually present in the
# document; matching is a case-insensitive substring test on the row label.
_COUNT_LABELS = (
    "number of", "receipts", "issues", "requisition", "items managed",
    "end strength", "workyear", "personnel", "manpower", "employee",
    "headcount", "fte", "sorties", "hours",
)


def _row_label(line: str) -> str:
    """The leading non-numeric text of a line (its row label)."""
    return re.match(r"^[^\d$(]*", line).group(0).strip()


def _is_count_row(label: str) -> bool:
    low = label.lower()
    return any(key in low for key in _COUNT_LABELS)


def _line_is_prose(line: str) -> bool:
    """True if a line reads like a sentence rather than a table row.

    A prose dollar amount is already absolute ("projects costing between
    $250,000 and $6,000,000") and must not be multiplied by a page banner.
    The reliable, position-independent tell is the shape of the whole line:
    prose is word-heavy with only a number or two, whereas a financial table
    row is a short label followed by several numeric columns (FY23/FY24/FY25).
    Judging the line as a whole (not the token's immediate neighbours) is what
    makes this robust when a sentence wraps and a value lands at a line edge.
    """
    words = len(re.findall(r"[A-Za-z]{2,}", line))
    numbers = len(_NUMBER.findall(line))
    if words < 6 or numbers > 2:
        return False
    # A financial table row puts its values in trailing columns, so it ENDS
    # with a number; running prose ends with a word or punctuation. Requiring
    # this also rescues doubled-label rows where the text layer has mangled one
    # value into the repeated label (dropping the line's clean-number count):
    # e.g. "Accumulated Operating Result (AOR) ...(AO54R6).10 73.1 547.8".
    ends_with_number = bool(re.search(r"\d[)%.]*\s*$", line))
    return not ends_with_number


def iter_candidates(page_text: str, page_number: int) -> Iterator[Candidate]:
    """Yield every numeric Candidate on a page, line by line."""
    for line in page_text.split("\n"):
        label = _row_label(line)
        count_row = _is_count_row(label)
        prose_line = _line_is_prose(line)
        for match in _NUMBER.finditer(line):
            token = match.group(0)
            face = _parse_face(token)
            if face is None:
                continue

            # Look at what immediately follows the number: a magnitude
            # suffix/word, or letters it is glued to (an identifier).
            tail = line[match.end():]
            inline_mult: Optional[int] = None
            inline_label = ""
            glued = False
            suffix = _SUFFIX_RE.match(tail)
            word = _WORD_RE.match(tail)
            if suffix:
                inline_mult = _SUFFIX_MULT[suffix.group(1)]
                inline_label = suffix.group(1)
            elif word:
                inline_mult = _WORD_MULT[word.group(1).lower()]
                inline_label = word.group(1).lower()
            elif re.match(r"[A-Za-z]", tail):
                glued = True  # digits run straight into letters -> identifier

            digits = re.sub(r"[^\d]", "", token)
            yield Candidate(
                face=face,
                token=_clean_token(token),
                page=page_number,
                line=line,
                inline_mult=inline_mult,
                inline_label=inline_label,
                is_identifier=_classify_identifier(digits, token, glued),
                is_count_row=count_row,
                in_prose=prose_line,
            )


# ===========================================================================
# Stage 4 — Scale resolution (the heart of the program)
# ===========================================================================
# Decide the multiplier for one candidate, in strict priority order.  Returns
# (multiplier, human-readable reason).  The reason feeds the provenance output
# so every winner can be explained.

def resolve_multiplier(cand: Candidate, scale: Scale) -> tuple[int, str]:
    # Tier 1 — inline magnitude wins and stops.  The number already carries
    # its own magnitude ("$234M", "$9.6 billion"); applying a page banner too
    # would double-count.
    if cand.inline_mult is not None:
        return cand.inline_mult, f"inline magnitude '{cand.inline_label}'"

    # Tier 3 (early-out) — no governing banner: value stands as printed.
    if scale.multiplier == 1:
        return 1, "no scale banner on page"

    # --- Tier 2 — a banner governs.  It scales only genuine figures OF ITS
    #     UNIT.  The three checks below are CORRECTNESS guards: they keep the
    #     program from reporting magnitudes the document never states.  They
    #     stay on in BOTH identifier modes.

    # (a) An identifier is a label written in digits, not a dollar amount, so a
    #     dollar banner does not scale it.  (This also neutralises the doubled
    #     "(3300)" appropriation code that the text layer mangles into a large
    #     ungrouped run — even when identifiers are *included* as raw values.)
    if cand.is_identifier:
        return 1, f"identifier — not a {scale.unit} amount"

    # (b) A dollar banner does not scale a non-dollar quantity (receipts,
    #     personnel, end strength, hours) sharing the table.
    if scale.unit == "dollars" and cand.is_count_row:
        return 1, "non-dollar quantity under a dollar banner"

    # (c) A dollar amount written out in a sentence is already absolute
    #     ("$6,000,000"); the banner applies to tabular cells, not prose.
    if scale.unit == "dollars" and cand.in_prose:
        return 1, "absolute amount in prose"

    # Otherwise it is a tabular figure of the banner's unit -> scale it.
    return scale.multiplier, f"page banner {scale.label}"


# ===========================================================================
# Stage 5 — Track the two maxima with provenance
# ===========================================================================

@dataclass
class Winner:
    value: float
    page: int
    token: str
    multiplier: int
    reason: str
    snippet: str


def _snippet(cand: Candidate, width: int = 48) -> str:
    """A short window of the source line around the token, for provenance."""
    text = " ".join(cand.line.split())  # collapse whitespace
    # Re-locate the token in the cleaned line (best-effort) for a tidy window.
    idx = text.find(cand.token)
    if idx == -1:
        return text[:2 * width].strip()
    lo = max(0, idx - width)
    hi = min(len(text), idx + len(cand.token) + width)
    return ("..." if lo else "") + text[lo:hi].strip() + ("..." if hi < len(text) else "")


def find_maxima(pages: List[str], include_identifiers: bool) -> tuple[Winner, Winner]:
    """Scan all pages and return (raw_winner, scaled_winner)."""
    raw: Optional[Winner] = None
    scaled: Optional[Winner] = None

    for page_index, page_text in enumerate(pages):
        scale = detect_scale(page_text)
        for cand in iter_candidates(page_text, page_index + 1):
            # Identifier POLICY (the only thing the flag controls): by default
            # a pure identifier is not a "value" at all and is skipped for both
            # maxima; --include-identifiers lets it compete as a raw value.
            if cand.is_identifier and not include_identifiers:
                continue

            # RAW maximum: the numeral exactly as printed.
            if raw is None or cand.face > raw.value:
                raw = Winner(cand.face, cand.page, cand.token, 1,
                             "value as printed", _snippet(cand))

            # SCALE-ADJUSTED maximum: numeral x resolved multiplier.
            mult, reason = resolve_multiplier(cand, scale)
            adjusted = cand.face * mult
            if scaled is None or adjusted > scaled.value:
                scaled = Winner(adjusted, cand.page, cand.token, mult,
                                reason, _snippet(cand))

    return raw, scaled


# ===========================================================================
# Command-line interface / reporting
# ===========================================================================

def _format_value(value: float) -> str:
    """Whole numbers without a trailing .0; fractional values kept readable."""
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.3f}".rstrip("0").rstrip(".")


def _print_winner(title: str, w: Winner) -> None:
    print(title)
    print(f"  value : {_format_value(w.value)}")
    print(f"  page  : {w.page}")
    if w.multiplier != 1:
        print(f"  scale : x{w.multiplier:,}  ({w.reason})")
    else:
        print(f"  scale : x1  ({w.reason})")
    print(f"  token : {w.token!r}")
    print(f"  text  : {w.snippet!r}")
    print()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Find the largest number in a budget PDF (raw and "
                    "scale-adjusted).")
    parser.add_argument(
        "pdf", nargs="?", default=DEFAULT_PDF,
        help="Path to the PDF (default: the bundled FY25 AFWCF document).")
    parser.add_argument(
        "--include-identifiers", action="store_true",
        help="Count pure numeric identifiers (stock numbers, program-element "
             "codes, phone numbers) as values. Off by default. Note: this "
             "never disables the scaling-correctness guards — identifiers are "
             "still never multiplied by a dollar banner.")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.pdf):
        print(f"Error: PDF not found at '{args.pdf}'.", file=sys.stderr)
        print("Pass a path as the first argument, or place the bundled file at "
              f"'{DEFAULT_PDF}'.", file=sys.stderr)
        return 1

    try:
        pages = extract_pages(args.pdf)
    except Exception as exc:  # pdfplumber raises various errors on bad input
        print(f"Error: could not read '{args.pdf}' as a PDF ({exc}).",
              file=sys.stderr)
        return 1

    raw, scaled = find_maxima(pages, args.include_identifiers)
    if raw is None or scaled is None:
        print("No numbers found in the document.")
        return 0

    mode = "included" if args.include_identifiers else "excluded (default)"
    print(f"File   : {args.pdf}")
    print(f"Pages  : {len(pages)} scanned   |   identifiers: {mode}")
    print("=" * 72)
    _print_winner("RAW maximum  (greatest value as literally printed)", raw)
    _print_winner("SCALE-ADJUSTED maximum  (greatest value after applying "
                  "document scale guidance)", scaled)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

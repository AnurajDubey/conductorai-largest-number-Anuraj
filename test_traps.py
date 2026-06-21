#!/usr/bin/env python3
"""Validation suite for find_largest.py.

Each test encodes one of the document's known traps — the cases where a naive
reader would invent a phantom maximum.  They double as living documentation of
*why* the winners are what they are.

Runs standalone:   python test_traps.py
Or under pytest:   pytest test_traps.py
"""
from __future__ import annotations

import os

import find_largest as fl

PDF = fl.DEFAULT_PDF


# Extract once and reuse across tests (the slow part).
_PAGES = fl.extract_pages(PDF)


def _candidate_for(token_substring: str, page: int) -> fl.Candidate:
    """Return the first candidate on `page` whose token contains the substring."""
    for cand in fl.iter_candidates(_PAGES[page - 1], page):
        if token_substring in cand.token:
            return cand
    raise AssertionError(f"token {token_substring!r} not found on page {page}")


def _multiplier_for(token_substring: str, page: int) -> int:
    cand = _candidate_for(token_substring, page)
    scale = fl.detect_scale(_PAGES[page - 1])
    return fl.resolve_multiplier(cand, scale)[0]


# --- Headline winners -------------------------------------------------------

def test_raw_maximum():
    raw, _ = fl.find_maxima(_PAGES, include_identifiers=False)
    assert raw.value == 6_000_000, raw
    assert raw.page == 93, raw


def test_scale_adjusted_maximum():
    _, scaled = fl.find_maxima(_PAGES, include_identifiers=False)
    assert scaled.value == 30_704_100_000, scaled
    assert scaled.page == 13 and scaled.multiplier == 1_000_000, scaled


def test_identifier_mode_does_not_change_the_winners():
    # The flag is a policy toggle, not a correctness switch: the phantoms it
    # could expose are still blocked by the scaling guards.
    raw_d, scl_d = fl.find_maxima(_PAGES, include_identifiers=False)
    raw_i, scl_i = fl.find_maxima(_PAGES, include_identifiers=True)
    assert raw_d.value == raw_i.value == 6_000_000
    assert scl_d.value == scl_i.value == 30_704_100_000


# --- Documented traps -------------------------------------------------------

def test_program_element_code_is_an_identifier():
    # "0708055F" — a Program Element id (letters attached, leading zero).
    cand = _candidate_for("0708055", page=92)
    assert cand.is_identifier is True


def test_prose_dollar_range_is_not_scaled():
    # "$250,000 and $6,000,000" sit in prose on a *thousands* page; they are
    # already absolute dollars and must keep multiplier 1 (else $6,000,000
    # would balloon to $6 billion).
    assert fl.detect_scale(_PAGES[92]).unit == "dollars"   # page 93 has a banner
    assert _multiplier_for("$6,000,000", page=93) == 1
    assert _multiplier_for("$250,000", page=93) == 1


def test_receipts_count_is_not_scaled():
    # "Number of Receipts 1,754,801" under a millions banner is a COUNT.
    assert fl.detect_scale(_PAGES[28]).multiplier == 1_000_000   # page 29
    assert _multiplier_for("1,754,801", page=29) == 1


def test_end_strength_count_is_not_scaled():
    # Headcount on a millions page would otherwise scale to $35B and beat the
    # real winner ($30.7B) — the most dangerous count in the document.
    assert _multiplier_for("35,110", page=13) == 1


def test_inline_magnitude_is_not_double_scaled():
    # "$78.0M" / "$234M" already carry their magnitude; the page banner must
    # not be applied on top.  Every inline-"M" amount on the page must resolve
    # to exactly x1,000,000 (from the suffix), never x1,000,000,000,000.
    scale = fl.detect_scale(_PAGES[31])  # page 32 is a millions page
    inline = [c for c in fl.iter_candidates(_PAGES[31], 32) if c.inline_label == "M"]
    assert inline, "expected inline $..M amounts on page 32"
    assert all(fl.resolve_multiplier(c, scale)[0] == 1_000_000 for c in inline)


def test_identifier_classification():
    import re

    def is_id(token: str) -> bool:
        return fl._classify_identifier(re.sub(r"[^\d]", "", token), token, False)

    # Identifiers: leading-zero code, long un-grouped run.
    assert is_id("0708055") and is_id("01234") and is_id("123456")
    # Values: fractional (incl. leading-zero decimals) and comma-grouped numbers.
    assert not is_id("0.5") and not is_id("0.000")
    assert not is_id("6,000,000") and not is_id("30,704.1") and not is_id("2025")


def test_no_phantom_above_one_trillion_in_either_mode():
    # The doubled-text artifacts on p32 ("(3330000)") and p56 (interleaved
    # label/number) must never produce a >= 1-trillion candidate.  The true
    # scaled winner is ~3.07e10; anything near 1e12 would be a phantom.
    for include in (False, True):
        _, scaled = fl.find_maxima(_PAGES, include_identifiers=include)
        assert scaled.value < 1e12, (include, scaled)


def _run_standalone() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {test.__name__}: {exc}")
    print("-" * 60)
    print(f"{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    if not os.path.isfile(PDF):
        raise SystemExit(f"Bundled PDF not found at {PDF}")
    raise SystemExit(_run_standalone())

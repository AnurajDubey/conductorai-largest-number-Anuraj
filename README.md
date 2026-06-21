# Largest number in a budget PDF

Finds the largest number in a large US Air Force budget PDF and reports **two**
independent maxima:

1. **Raw max** — the greatest numerical value exactly as printed.
2. **Scale-adjusted max** — the greatest value after applying the document's own
   natural-language scale guidance (a table headed *"(Dollars in Millions)"*
   means `3.15` represents `3,150,000`).

The two winners come from different places in the document and are tracked
separately.

## Results on the bundled document

```
RAW maximum  (greatest value as literally printed)
  value : 6,000,000
  page  : 93
  scale : x1  (value as printed)
  token : '$6,000,000'
  text  : '$250,000 and $6,000,000) and are designed, scheduled, and constructed i...'

SCALE-ADJUSTED maximum  (greatest value after applying document scale guidance)
  value : 30,704,100,000
  page  : 13
  scale : x1,000,000  (page banner (Dollars in Millions))
  token : '30,704.1'
  text  : 'Total Revenue Total Revenue 28,239.2 29,176.6 30,704.1'
```

- The raw winner is the upper bound of a dollar **range written out in prose**
  (`$6,000,000`). It is already in full dollars, so it is *not* re-scaled.
- The scaled winner is **Total Revenue of \$30.7 billion** (`30,704.1` in a
  "Dollars in Millions" table → `× 1,000,000`), a realistic figure for the fund.

Every result prints its **provenance** (page, source text, token and the
multiplier applied with a reason) so the winner can always be explained and
checked against the document.

## How to run

```bash
# 1. Install the one dependency (optionally inside a virtualenv)
python3 -m pip install -r requirements.txt

# 2. Run against the bundled PDF (default)
python3 find_largest.py

# 3. ...or against any other PDF
python3 find_largest.py path/to/another.pdf

# 4. Include pure numeric identifiers as values (see below)
python3 find_largest.py --include-identifiers
```

Requires Python 3.9+. Using `python3 -m pip` instead of a bare `pip`/`pip3`
avoids ambiguity on machines with multiple Python installs (e.g. system Python
vs. conda vs. pyenv): it always installs into the same interpreter that runs
the script. A bad or missing path prints a readable message rather than a
traceback. There are no network calls — the document is read locally.

### Tests

```bash
python3 test_traps.py       # standalone, prints PASS/FAIL
# or: pytest test_traps.py
```

`test_traps.py` encodes the **core happy path** (a plain `3.15` under a
millions banner → `3,150,000`), each **known trap** (below), and the **parse
conventions** (e.g. accounting-negative parentheses) as assertions, so the
correctness properties are explicit and re-checkable.

## Design

The pipeline is five small stages (mirrored by the sections in
[find_largest.py](find_largest.py)):

1. **Extract** text per page — the only part that touches pdfplumber, kept thin
   so the library could be swapped.
2. **Detect scale** per page — find the governing banner
   (`(Dollars in Millions)`, `($ Millions)`, `($M)`, `(Dollars in Thousands)`,
   `(Hours in Thousands)`), matched case-insensitively with tolerant spacing.
3. **Extract candidates** — tokenise numbers and parse them to floats.
4. **Resolve scale** — decide each number's multiplier (the heart of it).
5. **Track maxima** — keep the two running winners with provenance.

### Scale resolution (three tiers, in priority order)

1. **Inline magnitude wins and stops.** If a number carries its own magnitude —
   a `M`/`K`/`B` suffix or a `million`/`billion` word (`$234M`, `$9.6 billion`) —
   that is used and **no page banner is applied** (prevents double-scaling).
2. **Unit-matched page banner.** Otherwise, a dollar figure under a dollar
   banner is multiplied by the banner. A banner only scales numbers **of its
   unit** — a dollar banner does *not* scale a headcount or a receipts count in
   the same table.
3. **Raw (×1).** Anything else stands as printed.

To honour tier 2 without reconstructing table geometry, the program works
line-by-line, associates each number with its row label, and under a dollar
banner skips numbers that are: a **count/quantity row** (label contains
`Number of…`, `Receipts`, `End Strength`, `Workyears`, `Personnel`, `Hours`,
etc.), an **identifier** (see below), or an **absolute amount written in prose**
(a sentence, detected by it being word-heavy with only a number or two).

### Identifier policy — a deliberate, toggleable assumption

The task asks for the greatest *value*. A pure numeric **identifier** (stock
number, Program-Element code like `0708055F`, phone number) is a label written
in digits and has no value in that sense, so by **default it is excluded**.
`--include-identifiers` flips this.

Identifier tells: letters attached, a leading zero, or a long un-grouped
digit-run (well-formed values are comma-grouped and/or `$`-prefixed).

**Important:** the flag is a *policy* choice. The phantom guards — no
double-scaling, no scaling of non-dollar counts, no scaling of identifiers — are
*correctness* properties that prevent reporting magnitudes the document never
states. They stay **on in both modes**; the flag never disables them. (On this
document both modes give the same two winners.)

### Parsing conventions

A value in **accounting parentheses is read as negative** (`(302.3)` → `-302.3`,
`($78.0M)` → `-78,000,000`). For a *maximum* this is the safe reading: it can
only keep a parenthesised value from winning, never invent a large one. A
stray unbalanced parenthesis captured from sentence punctuation (`$6,000,000)`)
is tidied for display without changing the parsed value.

### Traps handled (validation cases)

| Trap | Page | Why it would mislead | Handling |
|------|------|----------------------|----------|
| `0708055F` | 92 | PE code looks like 708,055 | identifier → excluded by default |
| `$250,000` / `$6,000,000` | 93 | dollar range in prose under a *thousands* banner → ×1000 = \$6B phantom | prose amounts are absolute → not scaled |
| `1,754,801` (Number of Receipts) | 29 | a **count** under a millions banner → 1.75-trillion phantom | count row → not scaled |
| `35,110` (End Strength), Workyears, Items Managed | 13 / 29 | headcounts that would scale *above* the real winner | count rows → not scaled |
| `($78.0M)`, `$234M` | 32 | magnitude already baked in → double-scaled to 1e12+ | inline magnitude wins, banner not re-applied |
| `(3330000)` / interleaved label-number text | 32 / 56 | "fake bold" doubled text mangles `(3300)`/labels into huge ungrouped runs | strict token boundaries + identifier guard → never scaled, in either mode |

## Limitations and choices

- **Scale handling is page-scoped.** It works here because this document
  declares scale in page/block header text, not in individual column headers.
  For a document that declares scale *positionally* (e.g. different units per
  column), this would need to extend to column-level detection using word
  coordinates (pdfplumber exposes them) — a deliberate non-goal here, since this
  document does not require it.
- **Count detection is label-based.** The denylist targets the count/quantity
  row labels present in this document; an unusual count label could slip
  through. The guards are tuned for *precision on the extreme value* over recall
  on every cell — it matters most that the winner is real.
- **Scope.** No OCR (the text layer is clean), no second PDF library, no table
  reconstruction, no handling of scale variants the document does not contain,
  and no runtime fetching — all intentionally out of scope.

## Library choice

This build uses **pdfplumber** (MIT): readability is graded, the logic is
token-level (so word/line extraction is the right granularity), and no table
reconstruction is needed. For a production AI/RAG extraction pipeline I would
reach for **PyMuPDF + pymupdf4llm** (faster, Markdown-structured output),
weighing its **AGPL** license against pdfplumber's permissive **MIT**.

## The bundled document

`data/FY25_Air_Force_Working_Capital_Fund.pdf` (a public-domain US Government
budget, ~13 MB) is committed alongside the code so the program is reproducible
and fully self-contained offline.

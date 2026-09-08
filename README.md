# Albatross

A fact knowledge layer over PDFs. It turns pages into atomic claims anchored to
the pixels they came from, works out which claims are about the same thing, and
decides whether they agree, disagree, or only appear to disagree.

The hard part is the last one. Most figures that look contradictory are not.

---

## Quick start

```bash
python -m venv .venv && .venv/Scripts/activate    # source .venv/bin/activate on macOS/Linux
pip install -e .
albatross serve
```

Open http://127.0.0.1:8000. The built knowledge layer over six documents ships
in the repo, so nothing above needs an API key.

To ingest something new, put a free [AI Studio](https://aistudio.google.com/apikey)
key in `.env` as `GEMINI_API_KEY=...`, then upload through the UI or run the
pipeline directly:

```bash
albatross ingest report.pdf --first 20 --last 28   # page range optional
albatross extract && albatross resolve && albatross reconcile --judge
```

`albatross status` prints what is in the layer at any point.

---

# Architecture

## The comparison cell

Everything rests on one idea. Two numbers that differ are not in conflict unless
they measure the same thing under the same conditions.

Every fact lands in a **cell**: an entity, a predicate, and the qualifiers that
actually distinguish facts for that predicate. Comparison happens only inside a
cell, and the axes are discovered from the data rather than declared.

```mermaid
flowchart LR
    A["fact A"] --> Q{"same cell?"}
    B["fact B"] --> Q
    Q -->|"one qualifier differs"| V["VARIANT<br/>that qualifier is the reason"]
    Q -->|"a required qualifier<br/>is missing"| I["INSUFFICIENT CONTEXT"]
    Q -->|"yes"| T{"within tolerance?"}
    T -->|"yes"| C["CORROBORATED"]
    T -->|"no"| X["CONTRADICTION"]
```

Delhivery's FY24 revenue is 74,540.82 million rupees standalone and 81,415.38
million consolidated. They differ by 6.9 billion and are both correct, because
`basis` puts them in different cells. No rule in the codebase mentions revenue,
consolidation, or fiscal years.

`CONTRADICTION` is the hardest verdict to reach, deliberately. Asserting one
asserts that two facts measure the same thing, and the predicate registry
clusters by similarity, so that has to be earned rather than assumed.

## Pipeline

```mermaid
flowchart TD
    PDF["PDF"] --> ING["<b>ingest</b><br/>column-aware blocks + bboxes<br/>page content hash"]
    ING --> EXT["<b>extract</b><br/>one page per call<br/>claims cite block ids"]
    EXT --> GRD["<b>ground</b><br/>do the claim's own numbers<br/>appear in the blocks it cited?"]
    GRD --> RES["<b>resolve</b><br/>entity + predicate registries<br/>embeddings, unit and digit gates"]
    RES --> REC["<b>reconcile</b><br/>cells, comparators, tolerance"]
    REC --> JDG["<b>judge</b><br/>gated LLM pass<br/>for entity-valued pairs"]
    JDG --> UI["browse · filter · verify"]
```

| stage | in | out | model cost |
|---|---|---|---|
| ingest | PDF | blocks with bboxes, page hashes | none |
| extract | one page | claims citing block ids | 1 request/page |
| ground | claim + cited blocks | flags on unsupported literals | none |
| resolve | surface forms | canonical entities and predicates | 1 request/100 forms |
| reconcile | facts | verdicts with delta and tolerance | none |
| judge | gated pairs | verdicts for state changes | 1 request/8 pairs |

Only two stages touch a model, and both cache on the prompt, so a page is paid
for once and never again.

## One fact, end to end

The corroboration case, traced through every stage with its real values.

```mermaid
flowchart TD
    P["<b>page 22, annual report</b><br/>four-column spread"] --> BL
    BL["<b>block b12</b> at 62,604 → 297,652<br/>'FY24 stood at ₹81,415.38 million<br/>as against ₹72,253.01 million for FY23'"] --> CL
    CL["<b>claim</b><br/>subject Delhivery Limited<br/>predicate revenue from operations<br/>value 81415.38 · unit ₹ million<br/>qualifiers basis=consolidated, period=FY24<br/>block_ids [12]"] --> RG
    RG["<b>resolved</b><br/>ent_0000 · pre_0011<br/>10 predicate surface forms merged"] --> NM
    NM["<b>normalised</b><br/>81,415,380,000 INR<br/>tolerance ±5,000"] --> VD
    VD["<b>against the earnings deck</b><br/>'8,142 Cr' → 81,420,000,000<br/>tolerance ±5,000,000<br/>delta 4,620,000"] --> OUT
    OUT["<b>CORROBORATED</b><br/>92% of combined tolerance"]
```

Two links in that chain matter more than the rest.

The claim cites `b12` and nothing else, so its evidence is a bounding box.
`/evidence/{id}.png` crops exactly that region of exactly that page. Evidence is
a picture of the source, not a quotation of it.

Tolerance is derived, not configured. `8,142 Cr` asserts nothing finer than one
crore, so it carries ±5,000,000. The annual report prints two decimals of a
million, so it carries ±5,000. Their sum is what stops rounding from becoming
disagreement. A fixed epsilon would need tuning per predicate to do the same job.

## Why blocks, not text

`pdftotext -layout` on the prospectus board table returns the DIN column shifted
by two rows. Every director receives another director's identifier, and nothing
errors.

```
Sahil Barua                        01432123    ← belongs to Barasia
Sandeep Kumar Barasia              09431299    ← belongs to Colleran
```

No single reading order fixes this. Column-major repairs the annual report's
spliced four-column prose but decouples the table's names from their DINs.
Row-major does the reverse. On this corpus the two layouts are not separable by
geometry, because prose columns share a y coordinate exactly as table cells do.

So the extractor is handed coordinates instead of an order:

```
[b9  77,585 267,605] Deepak Kapoor / Chairman and Non-Executive Independent Director
[b19 287,585 325,595] 00162957
```

The model resolves the layout and cites the ids back, which is also what grounds
the claim. Verified against the annual report, which states the same DINs in
prose where table geometry cannot reach them.

## Storage

```mermaid
erDiagram
    DOCUMENTS ||--o{ PAGES : "content-hashed"
    PAGES ||--o{ BLOCKS : "bbox + text"
    DOCUMENTS ||--o{ FACTS : yields
    FACTS }o--|| REGISTRY : "resolves to"
    FACTS ||--o{ VERDICTS : "compared in"
```

Six tables, no domain vocabulary. `predicate` and every qualifier key are free
text, so a new kind of fact is new rows rather than a migration. Ingesting three
macroeconomics documents after three logistics ones grew the registries from 85
to 185 entities and 216 to 292 predicates with no code change.

Page content hashes make ingestion incremental. Re-uploading a document adds
nothing, and an overlapping page range adds only what is new.

## Stack

| layer | choice | why |
|---|---|---|
| PDF | PyMuPDF | blocks with bounding boxes, which grounding depends on |
| Storage | SQLite | 1,048 facts. Postgres costs the reader a container before they see anything |
| Vectors | 768-dim embeddings as JSON, numpy cosine | brute force over 1k vectors is ~20 ms. An index earns its cost near a million |
| Extract | `gemini-3.5-flash-lite` | free tier |
| Judge | `gemini-3.8-flash` | falls back on 429 or 503 |
| Web | FastAPI + Jinja2 | one process, no build step |

Four runtime dependencies and no model SDK. The whole model surface is one HTTP
POST.

---

## The four cases

**1. Corroborated across documents.** `81,415.38` million rupees in the annual
report against `8,142 Cr` in the earnings deck. Traced in full above.

**2. A likely contradiction.** None exists in either corpus, and that is the
finding rather than a gap.

The logistics documents once produced 211 numeric contradictions. Not one had
matching predicate wording on both sides, meaning every one came from two
different metrics the registry had merged. Requiring the documents to use the
same words turned all 211 into `INSUFFICIENT_CONTEXT`. Three audited filings
from one company do not contradict each other.

Three independent institutions were then ingested specifically to find a
disagreement. Twelve candidates, all explained: five entity over-merges, three
lost quarter markers, two unstated periods, and the two below.
`/verdicts?verdict=INSUFFICIENT_CONTEXT` is the rejection ledger, 343
comparisons declined with the rule that declined each one.

A unit test builds two facts in one cell with identical predicate wording and
values outside tolerance, and asserts the verdict is `CONTRADICTION`. The
machinery fires. The corpus has nothing to fire at.

**3. Explained by context.** The Economic Survey reports the current account
deficit at 1.2% of GDP in **Q2 FY25**. The IMF reports 0.2% in **2025Q2**. Two
independent institutions, apparently sixfold apart on the same quarter.

Different quarters. Q2 FY25 is Indian fiscal, July to September 2024. 2025Q2 is
calendar, April to June 2025. Period labels carry a convention marker, so the
verdict is `VARIANT_EXPLAINED_BY` with `QUALIFIER_DIFF:period` and the diff
recorded as `["2025Q2F", "2025Q2C"]`. The system does not claim to know either
convention's month boundaries, only that the labels are not interchangeable.

This was a false contradiction until the collision was found.

**4. An extraction failure.** The DIN column shift above, and a subtler one: a
claim recorded a resignation as effective August 04, 2023 while citing the block
that reads *"with effect from August 24, 2023"*. August 04 appears three times
elsewhere on that page. The claim was correctly grounded and factually wrong,
which is precisely what a block citation cannot catch.

So `grounding.py` checks that a claim's own numbers and dates appear in the
blocks it cites. About 2.7% of facts are flagged, and they are shown rather than
hidden.

---

## Decisions

Full record in `docs/decisions.md`, 39 numbered entries each with its cost. The
load-bearing ones:

| decision | cost accepted |
|---|---|
| Coordinates to the model, not a reading order | more tokens per page |
| Discriminating qualifiers found by value spread, not frequency | a frequency rule dropped `basis` and inverted case 1 into a contradiction |
| Missing a qualifier is not disagreeing on one | more `INSUFFICIENT_CONTEXT`, fewer confident answers |
| Contradiction requires identical predicate wording | genuine contradictions phrased differently are missed |
| Judge gated behind entity, document, and similarity filters | 40,000 pairs cut to 50, and recall now rides on one threshold |
| SQLite, no vector index | needs rebuilding above roughly 1M facts |
| Response cache in its own file | it once shared the knowledge DB, and a schema rebuild destroyed every paid response |

Claude Code (Opus) wrote the implementation from a written architecture
document, keeping a decisions log as it went. Gemini does extraction, embedding,
and pair judging at runtime. `docs/timeline.md` records each phase, what broke,
and what fixed it.

---

## Limitations

**Entity over-merge is real and visible.** "Delhivery Limited" absorbed its own
Board of Directors. "Kotak Mahindra Bank" absorbed "Kotak Mahindra Capital
Company". A content-token guard fixes names and is applied, but the same guard
cannot be applied to predicates without breaking case 1, whose surface forms sit
at cosine 0.721 and share almost no words. Per-entity contradiction density
would catch the rest, and is not built.

**One threshold is uncomfortably tight.** `Express Parcel revenue` sits at 0.7776
against a 0.78 predicate threshold, so a segment stays out of the total's cluster
by 0.0024. Two independent rules would have to fail for that to produce a wrong
answer and a test pins the split, but the margin is luck rather than design.

**Vision routing is designed, not built.** The prospectus contains a management
org chart whose names and titles exist only as pixels, so the entire
organisational structure is invisible to the pipeline. Highest-value gap.

**Silent restatement is undetectable.** A later document printing a different
figure with no correction language is indistinguishable from a real
disagreement.

**Unit parsing is a table, not a unit system.** It covers the magnitude and
currency words in these documents. An unlisted magnitude word would fail
quietly, which is the failure mode this system otherwise exists to prevent.

**Nothing learns from feedback.** Thresholds are constants justified by
measurement, not calibration.

---

## Tests

23 tests across 5 files, no test runner required.

```bash
python -B tests/test_reconcile.py
```

They pin what broke during the build: the DIN pairing, spliced columns,
incremental ingestion, the registry merges and splits that must and must not
happen, the four cases, and that a contradiction fires when one genuinely
exists.

---

## Notes

On Windows, prefix commands with `PYTHONIOENCODING=utf-8`. The corpus is full of
rupee signs and the console encoding will otherwise fail partway through a run.

`albatross.db` and `.llm-cache.db` are both committed. The cache holds 82 model
responses and 729 embeddings, so `resolve` and `reconcile` replay offline in
about three seconds. A rebuild from an empty database still needs a key for the
six document context cards, whose prompts depend on ingest order and so miss the
cache.

`docs/trail-run.md` verifies all of this by hand, every command executed with its
real output, including how to read an embedding out of the cache and reproduce a
clustering decision yourself.

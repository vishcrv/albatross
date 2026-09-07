"""Claim extraction: one page in, grounded atomic facts out.

Nothing here names a domain. The prompt asks for whatever the page asserts;
`predicate` and every `qualifiers[].key` are free text, so a new kind of fact
lands as new rows and needs no migration (docs/decisions.md D4).

Grounding is checked mechanically, not trusted: a claim citing a block id that
is not on the page is dropped.
"""
from __future__ import annotations

import json
import uuid

from . import grounding, llm, store

CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_text": {"type": "string"},
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "object_text": {"type": "string"},
                    "value": {"type": "number", "nullable": True},
                    "unit": {"type": "string", "nullable": True},
                    "value_type": {
                        "type": "string",
                        "enum": ["quantity", "date", "entity", "categorical", "boolean"],
                    },
                    "qualifiers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "value": {"type": "string"},
                            },
                            "required": ["key", "value"],
                        },
                    },
                    "modality": {
                        "type": "string",
                        "enum": ["asserted", "planned", "reported", "negated",
                                 "hypothetical"],
                    },
                    "approximate": {"type": "boolean"},
                    "confidence": {"type": "number"},
                    "block_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["claim_text", "subject", "predicate", "object_text",
                             "value_type", "qualifiers", "modality", "approximate",
                             "confidence", "block_ids"],
            },
        }
    },
    "required": ["claims"],
}

PROMPT = """\
You are extracting atomic, checkable facts from one page of a document.

The page is given as layout blocks. Each line is `[b<id> x0,y0 x1,y1] text`,
where the numbers are the block's bounding box on the page. Blocks that share a
vertical range are usually cells of the same table row - use the coordinates to
work out what belongs with what. Do not assume top-to-bottom reading order.

DOCUMENT CONTEXT
{context}

PAGE BLOCKS
{blocks}

Facts are not only numbers. Extract with equal care:

- quantities, amounts, counts, rates, dates;
- who holds or held a role, and in what capacity;
- state changes - an appointment, a resignation, a cessation, a merger, a
  renaming, a change of address - together with the date it took effect;
- relationships between organisations and people - ownership, subsidiary,
  nominee, auditor, registrar;
- statuses and classifications, including identifiers such as registration
  numbers.

A page of prose with no figures on it should still yield facts. If you find
yourself returning only numeric claims from a page that also narrates events,
you have missed the events.

Extract every substantive factual assertion. For each one:

- `claim_text`: a single self-contained sentence. Resolve every pronoun,
  "the Company", "the same period", "such services" into explicit text. If you
  cannot resolve a reference from this page or the context above, omit the
  claim entirely rather than guessing.
- `subject` / `predicate` / `object_text`: the thing, the relation, the value.
- `predicate` names the relation on its own, as a short noun phrase in the
  document's own wording. Everything that bounds the claim - period, basis,
  scope, segment, geography - goes in `qualifiers`. Worked example: from
  "revenue from operations on a standalone basis for FY24 stood at 74,540.82
  million", the predicate is "revenue from operations", the object is
  "74,540.82 million", and the qualifiers are basis=standalone and period=FY24.
  Two claims differing only in period or basis must produce the identical
  predicate string - that is what lets them be compared rather than silently
  passing each other by.

- `value` and `unit`: fill these only when the object is a quantity. Record the
  number exactly as printed, and the unit as printed ("INR million", "crore",
  "%", "Mn tons"). Do not convert.
- `qualifiers`: anything that bounds when, for whom, or on what basis the claim
  holds - reporting period, fiscal year, consolidated vs standalone, segment,
  geography, "as of" date. Invent keys as needed. If the page states a basis or
  a period explicitly, always record it; this is the difference between two
  figures disagreeing and two figures describing different things.
- `modality`: `negated` if the sentence denies the fact, `planned` for
  intentions, `reported` for something attributed to a third party.
- `approximate`: true for "about", "over", "~", "in excess of".
- `block_ids`: every block your claim came from. This is the evidence link.
  Cite only ids that appear above.

Prefer fewer, well-grounded claims to many vague ones. Skip boilerplate,
disclaimers, page furniture and table-of-contents lines. Return JSON.
"""


def extract_page(conn, doc_id: str, page_index: int, context: str = "") -> list[dict]:
    """Extract and store facts for one page. Cached on the prompt (D8)."""
    store.ensure_facts(conn)
    blocks = store.page_blocks(conn, doc_id, page_index)
    if not blocks:
        return []

    rendered = "\n".join(
        "[b{} {},{} {},{}] {}".format(
            b["ord"], round(b["x0"]), round(b["y0"]), round(b["x1"]), round(b["y1"]),
            b["text"].replace("\n", " / "),
        )
        for b in blocks
    )
    prompt = PROMPT.format(context=context or "(none given)", blocks=rendered)
    raw = llm.complete(prompt, schema=CLAIM_SCHEMA)

    by_ord = {b["ord"]: b["text"] for b in blocks}
    valid_ids = set(by_ord)
    kept: list[dict] = []
    for c in json.loads(raw).get("claims", []):
        cited = [i for i in c.get("block_ids", []) if i in valid_ids]
        if not cited:
            continue                      # ungrounded: drop, do not trust
        c["block_ids"] = cited
        cited_text = " ".join(by_ord[i] for i in cited)
        # Provenance is not accuracy - check the claim's own literals against
        # the blocks it points at. Flagged, never dropped: an inherited
        # qualifier legitimately fails this and is still worth keeping.
        c["grounding_issues"] = grounding.check(c, cited_text)
        kept.append(c)

    conn.executemany(
        "INSERT INTO facts (id, doc_id, page_index, claim_text, subject, predicate,"
        " object_text, value, unit, value_type, qualifiers, modality, approximate,"
        " confidence, block_ids, grounding_issues)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (str(uuid.uuid4()), doc_id, page_index, c["claim_text"], c["subject"],
             c["predicate"], c["object_text"], c.get("value"), c.get("unit"),
             c["value_type"], json.dumps(c["qualifiers"]), c["modality"],
             int(c["approximate"]), c["confidence"], json.dumps(c["block_ids"]),
             json.dumps(c["grounding_issues"]))
            for c in kept
        ],
    )
    conn.execute(
        "UPDATE pages SET extracted=1 WHERE doc_id=? AND page_index=?",
        (doc_id, page_index),
    )
    conn.commit()
    return kept


CONTEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "issuer": {"type": "string"},
        "doc_type": {"type": "string"},
        "reporting_period": {"type": "string"},
        "as_of": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["issuer", "doc_type", "reporting_period", "summary"],
}

CONTEXT_PROMPT = """\
Below are the first pages of a document. Identify what the document IS, from
its own contents only. Do not use the filename.

Report the issuing organisation, the kind of document, the period it reports
on, and the date it speaks as of. If the document does not state something,
give an empty string rather than inferring it.

`summary` should be one sentence a reader could use to interpret any figure
from this document - it is attached to every fact extracted from it, as the
context a bare table cell lacks.

PAGES
{pages}
"""


def document_context(conn, doc_id: str, sample_pages: int = 2) -> str:
    """Derive the document context card once, from the document's own text.

    Facts inherit this. It is how "FY2023-24 Annual Report" gives an implied
    period to a table cell whose own sentence never states a year - and it is a
    guess, which is why §9 downgrades contradictions that rest on it.
    """
    row = conn.execute(
        "SELECT context FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    if row and row["context"]:
        return row["context"]

    idx = [r["page_index"] for r in conn.execute(
        "SELECT page_index FROM pages WHERE doc_id=? ORDER BY page_index LIMIT ?",
        (doc_id, sample_pages),
    )]
    text = "\n\n".join(
        "\n".join(b["text"] for b in store.page_blocks(conn, doc_id, i)) for i in idx
    )[:12000]

    raw = llm.complete(
        CONTEXT_PROMPT.format(pages=text), schema=CONTEXT_SCHEMA
    )
    d = json.loads(raw)
    card = (f"{d['issuer']} - {d['doc_type']}. Reporting period: "
            f"{d['reporting_period'] or 'not stated'}. "
            f"As of: {d.get('as_of') or 'not stated'}. {d['summary']}")
    conn.execute("UPDATE documents SET context=? WHERE id=?", (card, doc_id))
    conn.commit()
    return card


def extract_document(conn, doc_id: str, verbose: bool = True) -> int:
    ctx = document_context(conn, doc_id)
    pages = [r["page_index"] for r in conn.execute(
        "SELECT page_index FROM pages WHERE doc_id=? AND extracted=0"
        " ORDER BY page_index", (doc_id,),
    )]
    total = 0
    for i in pages:
        try:
            n = len(extract_page(conn, doc_id, i, ctx))
        except Exception as e:                      # one bad page must not
            print(f"  page {i}: FAILED {e}"[:160])  # abort the document
            continue
        total += n
        if verbose:
            print(f"  page {i}: {n} claims", flush=True)
    return total

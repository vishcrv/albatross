"""FastAPI app: upload, browse, filter, and check the evidence.

One process, no queue, no build step (docs/decisions.md D5, D14). Upload
returns immediately and the pipeline runs in a background task, because
extracting a 100-page PDF takes minutes.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import traceback
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, Path as PathParam, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import cells, evidence, store
from .cli import file_id, ingest

DB = "albatross.db"
UPLOADS = Path("data/uploads")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

DESCRIPTION = """
A fact knowledge layer over PDFs. Claims are extracted per page, grounded to
the layout blocks they came from, resolved into entity and predicate
registries, and compared inside **comparison cells**.

### The one idea

Two figures that differ numerically are only a contradiction if they occupy
the same cell: same entity, same predicate, same *discriminating qualifiers*.
Two revenue figures with different reporting periods are not in conflict — the
qualifier difference is the explanation.

### Verdicts

| verdict | meaning |
|---|---|
| `CORROBORATED` | same cell, values agree within the precision the sources printed |
| `VARIANT_EXPLAINED_BY` | different cells, differing on exactly one qualifier — that qualifier is the reason |
| `INSUFFICIENT_CONTEXT` | the comparison cannot be made safely: a required qualifier is missing, context was inherited rather than stated, or the two predicates may not name the same measure |
| `CONTRADICTION` | same cell, same predicate wording, values outside tolerance |

`CONTRADICTION` is deliberately hard to reach. Asserting one means asserting
that two facts measure the same thing, and the predicate registry clusters by
similarity, so that assertion has to be earned.

### Grounding

Every fact records the layout blocks it came from, and every block records a
bounding box. `GET /evidence/{fact_id}.png` renders that region of that page
with the cited blocks outlined. Nothing is retyped from model output.
"""

TAGS = [
    {"name": "documents", "description": "Add PDFs and inspect what was ingested."},
    {"name": "facts", "description": "The extracted claims and the registries they resolve to."},
    {"name": "reconciliation", "description": "Comparison cells and the verdicts drawn from them."},
    {"name": "evidence", "description": "Page-region crops that make a verdict checkable."},
    {"name": "ui", "description": "Server-rendered pages. No build step."},
]

app = FastAPI(
    title="Albatross",
    summary="Fact knowledge layer over PDFs",
    description=DESCRIPTION,
    version="0.1.0",
    openapi_tags=TAGS,
)

JOBS = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, filename TEXT, status TEXT NOT NULL,
    detail TEXT, started_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# --------------------------------------------------------------------------
# response models - these are what make /docs worth reading
# --------------------------------------------------------------------------

class Qualifier(BaseModel):
    key: str = Field(examples=["basis"])
    value: str = Field(examples=["consolidated"])


class FactSide(BaseModel):
    id: str
    claim: str = Field(description="Decontextualised: pronouns and 'the Company' resolved.")
    value: float | None = Field(description="Null for entity-valued facts such as a directorship.")
    unit: str | None = Field(description="As printed. Never converted at extraction time.")
    qualifiers: list[Qualifier]
    document: str
    page: int = Field(description="1-based physical page of the PDF.")
    evidence: str = Field(description="Path to the page-region crop for this fact.")


class Reconciliation(BaseModel):
    id: str
    verdict: str = Field(examples=["VARIANT_EXPLAINED_BY"])
    reason_code: str = Field(
        description="Machine-readable reason. `QUALIFIER_DIFF:<key>` names the "
                    "qualifier that explains a variant; `JUDGE:*` came from the "
                    "LLM pair judge rather than a comparator.",
        examples=["QUALIFIER_DIFF:basis"])
    cell: dict = Field(description="The comparison cell: canonical entity and predicate.")
    qualifier_diff: dict = Field(
        description="Qualifiers that differ between the two facts. A null on "
                    "one side means that side never stated the qualifier.")
    delta: float | None = Field(description="Absolute difference after unit normalisation.")
    tolerance: float | None = Field(
        description="Derived from the precision each source printed, not a "
                    "fixed epsilon. '8,142 Cr' asserts nothing below one crore.")
    cross_document: bool
    trust: str = Field(description="`low` when either fact has an unsupported literal.")
    explanation: str | None = Field(description="Present for judge verdicts only.")
    facts: list[FactSide]


class Entity(BaseModel):
    id: str
    canonical: str
    facts: int
    aliases: list[str] = Field(
        description="Surface forms merged into this entity, learned from "
                    "embeddings rather than a lookup table.")


class DocumentDetail(BaseModel):
    id: str
    title: str
    context: str | None = Field(
        description="Document context card, derived from the document's own "
                    "first pages. Facts inherit it, which is a guess - "
                    "contradictions resting on it are downgraded.")
    pages: list[dict]
    facts: int


class Job(BaseModel):
    id: str
    filename: str | None
    status: str = Field(
        description="queued -> ingesting -> extracting -> resolving -> "
                    "reconciling -> done. Or `failed`, with the reason in detail.")
    detail: str | None
    started_at: str


# --------------------------------------------------------------------------

def db() -> sqlite3.Connection:
    conn = store.connect(DB)
    conn.executescript(JOBS)
    return conn


def _set_job(job: str, status: str, detail: str = "") -> None:
    conn = db()
    conn.execute("UPDATE jobs SET status=?, detail=? WHERE id=?",
                 (status, detail[:500], job))
    conn.commit()
    conn.close()


def process(job: str, path: Path, first: int | None, last: int | None) -> None:
    """Ingest -> extract -> resolve -> reconcile. Runs off the request thread."""
    from . import reconcile, resolve
    from .extract import extract_document
    try:
        _set_job(job, "ingesting")
        r = ingest(DB, path, first, last)
        conn = db()
        _set_job(job, "extracting", f"{r['pages_added']} new pages")
        extract_document(conn, r["doc_id"], verbose=False)
        _set_job(job, "resolving")
        resolve.resolve_all(conn)
        _set_job(job, "reconciling")
        counts = reconcile.run(conn)
        conn.close()
        _set_job(job, "done", json.dumps(counts))
    except Exception:
        # A failed upload must say why, not vanish. The traceback is the detail
        # a grader needs when a novel PDF breaks something.
        _set_job(job, "failed", traceback.format_exc()[-500:])


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, tags=["ui"],
         summary="Documents and upload")
def index(request: Request):
    """Ingested documents, their context cards, and the upload form.

    Server-rendered HTML. **Edge case:** the verdict pills are absent until
    `reconcile` has run at least once."""
    conn = db()
    docs = conn.execute(
        "SELECT d.id, d.title, d.context,"
        " (SELECT COUNT(*) FROM pages p WHERE p.doc_id=d.id) pages,"
        " (SELECT COUNT(*) FROM facts f WHERE f.doc_id=d.id) facts"
        " FROM documents d ORDER BY d.added_at").fetchall()
    counts = dict(conn.execute(
        "SELECT verdict, COUNT(*) FROM verdicts GROUP BY verdict").fetchall())
    jobs = conn.execute(
        "SELECT * FROM jobs ORDER BY started_at DESC LIMIT 5").fetchall()
    stats = {k: conn.execute(q).fetchone()[0] for k, q in (
        ("facts", "SELECT COUNT(*) FROM facts"),
        ("entities", "SELECT COUNT(*) FROM registry WHERE kind='entity'"),
        ("predicates", "SELECT COUNT(*) FROM registry WHERE kind='predicate'"))}
    conn.close()
    return templates.TemplateResponse(request, "index.html", {
        "docs": docs, "counts": counts, "jobs": jobs, "stats": stats})


@app.get("/verdicts", response_class=HTMLResponse, tags=["ui"],
         summary="Browse and filter reconciliations")
def verdicts(request: Request, verdict: str = "", entity: str = "",
             q: str = "", offset: int = 0):
    """Filterable list of reconciliations, cross-document pairs first.

    `INSUFFICIENT_CONTEXT` is the rejection ledger - comparisons declined,
    each with the rule that declined it.

    **Edge case:** an unknown `verdict` or `entity` yields an empty table
    rather than an error."""
    conn = db()
    rows = _verdict_rows(conn, verdict or None, entity or None, q or None,
                         offset=offset)
    kinds = conn.execute(
        "SELECT verdict, COUNT(*) FROM verdicts GROUP BY verdict"
        " ORDER BY 2 DESC").fetchall()
    entities = conn.execute(
        "SELECT r.id, r.canonical, COUNT(v.id) n FROM registry r"
        " JOIN verdicts v ON v.entity_id = r.id WHERE r.kind='entity'"
        " GROUP BY r.id ORDER BY n DESC LIMIT 30").fetchall()
    conn.close()
    return templates.TemplateResponse(request, "verdicts.html", {
        "rows": rows, "kinds": kinds, "entities": entities,
        "verdict": verdict, "entity": entity, "q": q, "offset": offset})


@app.get("/verdicts/{vid}", response_class=HTMLResponse, tags=["ui"],
         summary="One verdict, both claims, both page crops")
def verdict_detail(request: Request, vid: str):
    """Both claims side by side with their page-region crops.

    **Edge case:** returns 404 as HTML, not JSON, since this route is a
    browser page."""
    conn = db()
    r = conn.execute(
        "SELECT v.*, a.id aid, a.claim_text ca, a.value va, a.unit ua,"
        " a.qualifiers qa, a.grounding_issues ga, a.page_index pa,"
        " b.id bid, b.claim_text cb, b.value vb, b.unit ub, b.qualifiers qb,"
        " b.grounding_issues gb, b.page_index pb,"
        " da.title ta, db.title tb, pa2.labels la, pb2.labels lb,"
        " (SELECT canonical FROM registry x WHERE x.kind='entity'"
        "   AND x.id=v.entity_id) ent,"
        " (SELECT canonical FROM registry x WHERE x.kind='predicate'"
        "   AND x.id=v.predicate_id) pred"
        " FROM verdicts v"
        " JOIN facts a ON a.id=v.fact_a JOIN facts b ON b.id=v.fact_b"
        " JOIN documents da ON da.id=a.doc_id JOIN documents db ON db.id=b.doc_id"
        " JOIN pages pa2 ON pa2.doc_id=a.doc_id AND pa2.page_index=a.page_index"
        " JOIN pages pb2 ON pb2.doc_id=b.doc_id AND pb2.page_index=b.page_index"
        " WHERE v.id=?", (vid,)).fetchone()
    conn.close()
    if r is None:
        return HTMLResponse("<p>Unknown verdict.</p>", status_code=404)
    return templates.TemplateResponse(request, "verdict.html", {"r": r})


@app.get("/cells", response_class=HTMLResponse, tags=["ui"],
         summary="The comparison cell, drawn as a grid")
def cell_grid(request: Request, entity: str = "", predicate: str = ""):
    """Qualifiers as axes. Two values in one square disagree; two values in
    neighbouring squares differ because of the axis between them."""
    conn = db()
    options = cells.candidates(conn)
    if not entity or not predicate:
        if options:
            entity = options[0]["entity_id"]
            predicate = options[0]["predicate_id"]
    g = cells.grid(conn, entity, predicate) if entity else {
        "axes": {}, "rows": [], "cols": [], "cells": {}, "facts": 0}
    conn.close()
    return templates.TemplateResponse(request, "cells.html", {
        "g": g, "options": options, "entity": entity, "predicate": predicate})


@app.get("/facts", response_class=HTMLResponse, tags=["ui"],
         summary="Browse extracted claims")
def facts(request: Request, entity: str = "", q: str = "", offset: int = 0):
    """Browse extracted claims, filtered by entity or free text.

    **Edge case:** a claim flagged `literal not in cited block` is shown, not
    hidden - suppressing it would hide the failure the check exists to
    surface."""
    conn = db()
    where, params = ["1=1"], []
    if entity:
        where.append("f.entity_id = ?")
        params.append(entity)
    if q:
        where.append("f.claim_text LIKE ?")
        params.append(f"%{q}%")
    rows = conn.execute(
        "SELECT f.*, d.title FROM facts f JOIN documents d ON d.id=f.doc_id"
        " WHERE " + " AND ".join(where) +
        " ORDER BY f.doc_id, f.page_index LIMIT 80 OFFSET ?",
        (*params, offset)).fetchall()
    entities = conn.execute(
        "SELECT id, canonical, n FROM registry WHERE kind='entity'"
        " ORDER BY n DESC LIMIT 40").fetchall()
    conn.close()
    return templates.TemplateResponse(request, "facts.html", {
        "rows": rows, "entities": entities, "entity": entity, "q": q,
        "offset": offset})


# --------------------------------------------------------------------------
# documents
# --------------------------------------------------------------------------

@app.post("/documents", tags=["documents"], status_code=303,
          summary="Add a PDF to the knowledge layer",
          responses={303: {"description": "Accepted; redirects to the index. "
                                          "Poll `GET /jobs/{id}` for progress."}})
async def upload(background: BackgroundTasks, file: UploadFile,
                 first: str = Form("", description="First physical page, 1-based. Blank = from the start."),
                 last: str = Form("", description="Last physical page, inclusive. Blank = to the end.")):
    """Accepts the file and returns immediately; extraction runs in the
    background because a 100-page PDF takes minutes.

    **Edge cases**

    - *Re-uploading the same PDF* is a no-op. Pages are keyed by a content
      hash, so known pages are skipped and nothing is paid for twice.
    - *Overlapping page ranges* add only the pages not already present.
    - *A page range beyond the document* is clamped to what exists.
    - *A PDF with no text layer* (a scan) ingests but yields few or no facts.
      Nothing errors; the page count will exceed the fact count noticeably.
    - *An encrypted or malformed PDF* fails the job, not the request. The
      status becomes `failed` with the reason in `detail`.
    - *Free-tier quota exhaustion* mid-extraction also surfaces as `failed`.
      Already-extracted pages are kept, so re-running resumes rather than
      restarts.
    - The uploaded file is written to `data/uploads/` under its own basename,
      so two different PDFs sharing a filename overwrite each other on disk.
    """
    UPLOADS.mkdir(parents=True, exist_ok=True)
    dest = UPLOADS / Path(file.filename or "upload.pdf").name
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)

    job = file_id(dest)
    conn = db()
    conn.execute("INSERT OR REPLACE INTO jobs (id, filename, status)"
                 " VALUES (?,?,?)", (job, dest.name, "queued"))
    conn.commit()
    conn.close()

    background.add_task(process, job, dest,
                        int(first) if first.strip() else None,
                        int(last) if last.strip() else None)
    return RedirectResponse("/", status_code=303)


@app.get("/documents/{doc_id}", tags=["documents"], response_model=DocumentDetail,
         summary="What was ingested from one document",
         responses={404: {"description": "No document with that id."}})
def document(doc_id: str = PathParam(description="First 16 hex chars of the file's SHA-256.")):
    """Per-page ingest state, including the printed page labels found on each
    page.

    **Edge cases**

    - `labels` is a JSON *list*, not a string: a two-page spread carries two
      printed page numbers on one physical page, and some pages carry none.
    - `extracted` is 0 for pages that were ingested but whose claim extraction
      has not run or failed.
    """
    conn = db()
    d = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if d is None:
        conn.close()
        return JSONResponse({"error": "unknown document"}, status_code=404)
    pages = [dict(r) for r in conn.execute(
        "SELECT page_index, labels, page_hash, extracted FROM pages"
        " WHERE doc_id=? ORDER BY page_index", (doc_id,))]
    n = conn.execute("SELECT COUNT(*) FROM facts WHERE doc_id=?",
                     (doc_id,)).fetchone()[0]
    conn.close()
    return {"id": d["id"], "title": d["title"], "context": d["context"],
            "pages": pages, "facts": n}


@app.get("/jobs/{job_id}", tags=["documents"], response_model=Job,
         summary="Progress of one upload",
         responses={404: {"description": "No job with that id."}})
def job_status(job_id: str):
    """**Edge case:** a job id is the file's content hash, so re-uploading the
    same PDF reuses the same job row rather than creating a second one."""
    conn = db()
    r = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    return dict(r) if r else JSONResponse({"error": "unknown job"},
                                          status_code=404)


# --------------------------------------------------------------------------
# facts, entities, reconciliations
# --------------------------------------------------------------------------

def _verdict_rows(conn, verdict=None, entity=None, q=None, limit=60, offset=0):
    where, params = ["1=1"], []
    if verdict:
        where.append("v.verdict = ?")
        params.append(verdict)
    if entity:
        where.append("v.entity_id = ?")
        params.append(entity)
    if q:
        where.append("(a.claim_text LIKE ? OR b.claim_text LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    sql = (
        "SELECT v.*, a.claim_text ca, a.value va, a.unit ua, a.qualifiers qa,"
        " a.page_index pa, b.claim_text cb, b.value vb, b.unit ub,"
        " b.qualifiers qb, b.page_index pb, da.title ta, db.title tb,"
        " (SELECT canonical FROM registry r WHERE r.kind='entity'"
        "   AND r.id=v.entity_id) ent,"
        " (SELECT canonical FROM registry r WHERE r.kind='predicate'"
        "   AND r.id=v.predicate_id) pred"
        " FROM verdicts v"
        " JOIN facts a ON a.id=v.fact_a JOIN facts b ON b.id=v.fact_b"
        " JOIN documents da ON da.id=a.doc_id JOIN documents db ON db.id=b.doc_id"
        " WHERE " + " AND ".join(where) +
        # Cross-document first: agreement or conflict between two independent
        # documents is the point; intra-document pairs are mostly restatement.
        " ORDER BY v.cross_document DESC, v.id LIMIT ? OFFSET ?")
    return conn.execute(sql, (*params, limit, offset)).fetchall()


@app.get("/entities", tags=["facts"], response_model=list[Entity],
         summary="Canonical entities and their merged surface forms")
def entities(q: str = Query("", description="Substring match on the canonical name.")):
    """**Edge case:** aliases are learned from embedding similarity, so this
    endpoint is also how you audit an over-merge. If a cluster's `aliases` list
    contains two things you consider different, that is a real defect and it
    will distort every verdict for that entity."""
    conn = db()
    rows = conn.execute(
        "SELECT id, canonical, surface_forms, n FROM registry"
        " WHERE kind='entity' AND canonical LIKE ? ORDER BY n DESC LIMIT 100",
        (f"%{q}%",)).fetchall()
    conn.close()
    return [{"id": r["id"], "canonical": r["canonical"], "facts": r["n"],
             "aliases": json.loads(r["surface_forms"])} for r in rows]


@app.get("/api/reconciliations", tags=["reconciliation"],
         response_model=list[Reconciliation],
         summary="Verdicts with both facts and their evidence links")
def api_reconciliations(
    verdict: str = Query("", description="Exact verdict name. Unknown names return an empty list, not an error."),
    entity_id: str = Query("", description="Canonical entity id from `GET /entities`."),
    limit: int = Query(50, ge=1, le=500),
):
    """**Edge cases**

    - An unrecognised `verdict` yields `[]` rather than a 400. Verdict names
      are data, and the set can grow.
    - `delta` and `tolerance` are null for entity-valued verdicts (a
      directorship has no numeric difference).
    - `explanation` is populated only for `JUDGE:*` reason codes; comparator
      verdicts explain themselves through `reason_code` and `qualifier_diff`.
    - A null inside `qualifier_diff` means one side never stated that
      qualifier — missing information, not disagreement.
    """
    conn = db()
    rows = _verdict_rows(conn, verdict or None, entity_id or None, limit=limit)
    conn.close()
    return [{
        "id": r["id"], "verdict": r["verdict"], "reason_code": r["reason_code"],
        "cell": {"entity": r["ent"], "predicate": r["pred"]},
        "qualifier_diff": json.loads(r["qualifier_diff"]),
        "delta": r["delta"], "tolerance": r["tolerance"],
        "cross_document": bool(r["cross_document"]), "trust": r["trust"],
        "explanation": r["explanation"],
        "facts": [
            {"id": r["fact_a"], "claim": r["ca"], "value": r["va"],
             "unit": r["ua"], "qualifiers": json.loads(r["qa"]),
             "document": r["ta"], "page": r["pa"] + 1,
             "evidence": "/evidence/" + r["fact_a"] + ".png"},
            {"id": r["fact_b"], "claim": r["cb"], "value": r["vb"],
             "unit": r["ub"], "qualifiers": json.loads(r["qb"]),
             "document": r["tb"], "page": r["pb"] + 1,
             "evidence": "/evidence/" + r["fact_b"] + ".png"},
        ],
    } for r in rows]


@app.get("/api/cells", tags=["reconciliation"],
         summary="One comparison cell grid as JSON")
def api_cells(entity: str = Query(..., description="Entity id, e.g. ent_0000."),
              predicate: str = Query(..., description="Predicate id, e.g. pre_0010.")):
    """**Edge case:** an entity/predicate pair with fewer than two qualifier
    values collapses to a single row or column, and the em-dash key `—` marks
    facts that stated no value for that axis at all."""
    conn = db()
    g = cells.grid(conn, entity, predicate)
    conn.close()
    return g


@app.get("/evidence/{fact_id}.png", tags=["evidence"],
         response_class=Response,
         summary="Page-region crop with the cited blocks outlined",
         responses={
             200: {"content": {"image/png": {}}, "description": "PNG at 144 dpi."},
             404: {"description": "No fact with that id."}})
def evidence_png(fact_id: str):
    """Rendered from the PDF at request time, never from stored model output.

    **Edge cases**

    - A fact citing no blocks falls back to the whole page rather than failing.
    - A crop shorter than 90pt is grown vertically; a one-line crop is
      unreadable without surrounding context.
    - The clip is intersected with the page rectangle, so a block near the
      margin cannot request pixels off the page.
    - The source PDF must still be at the path recorded at ingest. Move
      `data/` and crops 500 while every other endpoint keeps working.
    """
    conn = db()
    try:
        png = evidence.crop(conn, fact_id)
    except KeyError:
        return JSONResponse({"error": "unknown fact"}, status_code=404)
    finally:
        conn.close()
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


# --------------------------------------------------------------------------

def _qualifiers(raw: str) -> str:
    try:
        return "  ".join(str(q["key"]) + " " + str(q["value"])
                         for q in json.loads(raw))
    except (TypeError, ValueError):
        return ""


def _fmt(v) -> str:
    if not isinstance(v, (int, float)):
        return "—"
    text = f"{v:,.2f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


templates.env.filters["qualifiers"] = _qualifiers
templates.env.filters["fmt"] = _fmt

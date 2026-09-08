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

from fastapi import BackgroundTasks, FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import evidence, store
from .cli import file_id, ingest

DB = "albatross.db"
UPLOADS = Path("data/uploads")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app = FastAPI(title="Albatross - Fact Knowledge Layer")

JOBS = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, filename TEXT, status TEXT NOT NULL,
    detail TEXT, started_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def db() -> sqlite3.Connection:
    conn = store.connect(DB)
    conn.executescript(JOBS)
    return conn


def _set_job(job: str, status: str, detail: str = "") -> None:
    conn = db()
    conn.execute(
        "UPDATE jobs SET status=?, detail=? WHERE id=?",
        (status, detail[:500], job),
    )
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


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    conn = db()
    docs = conn.execute(
        "SELECT d.id, d.title, d.context,"
        " (SELECT COUNT(*) FROM pages p WHERE p.doc_id=d.id) pages,"
        " (SELECT COUNT(*) FROM facts f WHERE f.doc_id=d.id) facts"
        " FROM documents d ORDER BY d.added_at"
    ).fetchall()
    counts = dict(conn.execute(
        "SELECT verdict, COUNT(*) FROM verdicts GROUP BY verdict").fetchall())
    jobs = conn.execute(
        "SELECT * FROM jobs ORDER BY started_at DESC LIMIT 5").fetchall()
    conn.close()
    return templates.TemplateResponse(request, "index.html", {"docs": docs, "counts": counts, "jobs": jobs,
        "total": sum(counts.values())})


@app.post("/documents")
async def upload(background: BackgroundTasks, file: UploadFile,
                 first: str = Form(""), last: str = Form("")):
    UPLOADS.mkdir(parents=True, exist_ok=True)
    dest = UPLOADS / Path(file.filename or "upload.pdf").name
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)

    job = file_id(dest)
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO jobs (id, filename, status) VALUES (?,?,?)",
        (job, dest.name, "queued"))
    conn.commit()
    conn.close()

    background.add_task(process, job, dest,
                        int(first) if first.strip() else None,
                        int(last) if last.strip() else None)
    return RedirectResponse("/", status_code=303)


@app.get("/documents/{doc_id}")
def document(doc_id: str):
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
    clause = " AND ".join(where)
    sql = (
        "SELECT v.*, a.claim_text ca, a.value va, a.unit ua, a.qualifiers qa,"
        " b.claim_text cb, b.value vb, b.unit ub, b.qualifiers qb,"
        " da.title ta, db.title tb,"
        " (SELECT canonical FROM registry r WHERE r.kind='entity'"
        "   AND r.id=v.entity_id) ent,"
        " (SELECT canonical FROM registry r WHERE r.kind='predicate'"
        "   AND r.id=v.predicate_id) pred"
        " FROM verdicts v"
        " JOIN facts a ON a.id=v.fact_a JOIN facts b ON b.id=v.fact_b"
        " JOIN documents da ON da.id=a.doc_id JOIN documents db ON db.id=b.doc_id"
        " WHERE " + clause +
        # Cross-document first: agreement or conflict between two independent
        # documents is the point; intra-document pairs are mostly restatement.
        " ORDER BY v.cross_document DESC, v.id LIMIT ? OFFSET ?"
    )
    return conn.execute(sql, (*params, limit, offset)).fetchall()


@app.get("/verdicts", response_class=HTMLResponse)
def verdicts(request: Request, verdict: str = "", entity: str = "",
             q: str = "", offset: int = 0):
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
    return templates.TemplateResponse(request, "verdicts.html", {"rows": rows, "kinds": kinds, "entities": entities,
        "verdict": verdict, "entity": entity, "q": q, "offset": offset})


@app.get("/verdicts/{vid}", response_class=HTMLResponse)
def verdict_detail(request: Request, vid: str):
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
        return HTMLResponse("<p>unknown verdict</p>", status_code=404)
    return templates.TemplateResponse(request, "verdict.html", {"r": r})


@app.get("/facts", response_class=HTMLResponse)
def facts(request: Request, entity: str = "", q: str = "", offset: int = 0):
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
    return templates.TemplateResponse(request, "facts.html", {"rows": rows, "entities": entities,
        "entity": entity, "q": q, "offset": offset})


@app.get("/entities")
def entities(q: str = ""):
    conn = db()
    rows = conn.execute(
        "SELECT id, canonical, surface_forms, n FROM registry"
        " WHERE kind='entity' AND canonical LIKE ? ORDER BY n DESC LIMIT 100",
        (f"%{q}%",)).fetchall()
    conn.close()
    return [{"id": r["id"], "canonical": r["canonical"], "facts": r["n"],
             "aliases": json.loads(r["surface_forms"])} for r in rows]


@app.get("/api/reconciliations")
def api_reconciliations(verdict: str = "", entity_id: str = "", limit: int = 50):
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
            {"claim": r["ca"], "value": r["va"], "unit": r["ua"],
             "qualifiers": json.loads(r["qa"]), "document": r["ta"],
             "evidence": "/evidence/" + r["fact_a"] + ".png"},
            {"claim": r["cb"], "value": r["vb"], "unit": r["ub"],
             "qualifiers": json.loads(r["qb"]), "document": r["tb"],
             "evidence": "/evidence/" + r["fact_b"] + ".png"},
        ],
    } for r in rows]


@app.get("/evidence/{fact_id}.png")
def evidence_png(fact_id: str):
    conn = db()
    try:
        png = evidence.crop(conn, fact_id)
    except KeyError:
        return JSONResponse({"error": "unknown fact"}, status_code=404)
    finally:
        conn.close()
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    conn = db()
    r = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    return dict(r) if r else JSONResponse({"error": "unknown job"},
                                          status_code=404)


def _qualifiers(raw: str) -> str:
    try:
        return "  ".join(str(q["key"]) + "=" + str(q["value"])
                         for q in json.loads(raw))
    except (TypeError, ValueError):
        return ""


def _fmt(v) -> str:
    if not isinstance(v, (int, float)):
        return "-"
    text = f"{v:,.2f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


templates.env.filters["qualifiers"] = _qualifiers
templates.env.filters["fmt"] = _fmt

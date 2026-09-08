"""Ingest CLI.

Page selection is an argument, never a rule. The system's own answer to "which
pages do I process" is "all of them, whatever you are handed" - a page list
inside src/ would be the document-specific rule the brief bans (D10).
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from . import store
from .pdf import read_pages


def file_id(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def ingest(db: str, pdf: Path, first: int | None, last: int | None) -> dict:
    conn = store.connect(db)
    doc_id = file_id(pdf)
    store.add_document(conn, doc_id, pdf, title=pdf.stem)

    added = skipped = 0
    try:
        for page in read_pages(str(pdf), first, last):
            if store.add_page(conn, doc_id, page):
                added += 1
            else:
                skipped += 1
        conn.commit()
    finally:
        conn.close()
    return {"doc_id": doc_id, "pages_added": added, "pages_skipped": skipped}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="albatross")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="read a PDF into the knowledge layer")
    ing.add_argument("pdf", type=Path)
    ing.add_argument("--first", type=int, help="first physical page, 1-based")
    ing.add_argument("--last", type=int, help="last physical page, inclusive")
    ing.add_argument("--db", default="albatross.db")

    ex = sub.add_parser("extract", help="extract claims from ingested pages")
    ex.add_argument("--db", default="albatross.db")

    rs = sub.add_parser("resolve", help="build entity and predicate registries")
    rs.add_argument("--db", default="albatross.db")

    rc = sub.add_parser("reconcile", help="compare facts and record verdicts")
    rc.add_argument("--db", default="albatross.db")
    rc.add_argument("--judge", action="store_true",
                    help="also run the LLM pair judge on gated non-numeric pairs")

    st = sub.add_parser("status", help="what is in the knowledge layer")
    st.add_argument("--db", default="albatross.db")

    args = ap.parse_args(argv)
    if args.cmd == "ingest":
        r = ingest(args.db, args.pdf, args.first, args.last)
        print(f"{args.pdf.name}: +{r['pages_added']} pages"
              f" ({r['pages_skipped']} already known)  doc={r['doc_id']}")
    elif args.cmd == "resolve":
        from .resolve import resolve_all
        conn = store.connect(args.db)
        r = resolve_all(conn)
        print(f"{r['entities']} entities, {r['predicates']} predicates")
        conn.close()
    elif args.cmd == "reconcile":
        from . import reconcile
        conn = store.connect(args.db)
        counts = reconcile.run(conn)
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {v:>5}  {k}")
        if args.judge:
            from . import judge
            print("judging non-numeric pairs...")
            print("  ", judge.run_and_store(conn))
        conn.close()
    elif args.cmd == "extract":
        from .extract import extract_document
        conn = store.connect(args.db)
        docs = [r["id"] for r in conn.execute(
            "SELECT id FROM documents ORDER BY added_at")]
        for d in docs:
            title = conn.execute(
                "SELECT title FROM documents WHERE id=?", (d,)).fetchone()["title"]
            print(title)
            print(f"  -> {extract_document(conn, d)} claims")
        conn.close()
    else:
        conn = store.connect(args.db)
        rows = conn.execute(
            "SELECT d.title,"
            "  (SELECT COUNT(*) FROM pages  p WHERE p.doc_id = d.id) n,"
            "  (SELECT COUNT(*) FROM blocks b WHERE b.doc_id = d.id) nb"
            " FROM documents d ORDER BY d.added_at"
        ).fetchall()
        conn.close()
        for row in rows:
            print(f"{row['n']:>4} pages  {row['nb']:>6} blocks  {row['title']}")


if __name__ == "__main__":
    main()

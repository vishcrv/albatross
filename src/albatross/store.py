"""SQLite store. Schema is fixed, tiny and domain-free.

Nothing here names a metric, a company or a document type. Domain specificity
lives in rows - free-text predicates and open-ended qualifiers - so a new kind
of fact needs no migration. See docs/decisions.md D4.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,          -- sha256 of file bytes
    path        TEXT NOT NULL,
    title       TEXT,
    context     TEXT,                      -- document context card, JSON
    added_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pages (
    doc_id      TEXT NOT NULL REFERENCES documents(id),
    page_index  INTEGER NOT NULL,          -- 0-based physical
    labels      TEXT NOT NULL,             -- JSON list; a spread has several
    page_hash   TEXT NOT NULL,
    width       REAL NOT NULL,
    height      REAL NOT NULL,
    route       TEXT NOT NULL,             -- text | vision
    extracted   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (doc_id, page_index)
);

-- Re-ingesting an unchanged page must be a no-op, and extraction is paid for
-- once per unique page ever, not once per run (D8).
CREATE UNIQUE INDEX IF NOT EXISTS pages_by_hash ON pages(page_hash, doc_id);

CREATE TABLE IF NOT EXISTS blocks (
    doc_id      TEXT NOT NULL,
    page_index  INTEGER NOT NULL,
    ord         INTEGER NOT NULL,
    x0 REAL, y0 REAL, x1 REAL, y1 REAL,
    text        TEXT NOT NULL,
    PRIMARY KEY (doc_id, page_index, ord)
);
"""


def connect(path: str | Path = "albatross.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def add_document(conn, doc_id: str, path: str, title: str | None = None) -> bool:
    """Returns True if newly inserted, False if already present."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO documents (id, path, title) VALUES (?,?,?)",
        (doc_id, str(path), title),
    )
    return cur.rowcount > 0


def add_page(conn, doc_id: str, page) -> bool:
    """Store one page and its blocks. False if this page hash is already known."""
    seen = conn.execute(
        "SELECT 1 FROM pages WHERE page_hash=? AND doc_id=?", (page.page_hash, doc_id)
    ).fetchone()
    if seen:
        return False
    conn.execute(
        "INSERT OR REPLACE INTO pages"
        " (doc_id, page_index, labels, page_hash, width, height, route)"
        " VALUES (?,?,?,?,?,?,?)",
        (doc_id, page.index, json.dumps(page.labels), page.page_hash,
         page.width, page.height, "text"),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO blocks"
        " (doc_id, page_index, ord, x0, y0, x1, y1, text) VALUES (?,?,?,?,?,?,?,?)",
        [(doc_id, page.index, b.ord, *b.bbox, b.text) for b in page.blocks],
    )
    return True


FACTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id           TEXT PRIMARY KEY,
    doc_id       TEXT NOT NULL,
    page_index   INTEGER NOT NULL,
    claim_text   TEXT NOT NULL,
    subject      TEXT NOT NULL,
    predicate    TEXT NOT NULL,
    object_text  TEXT NOT NULL,
    value        REAL,                      -- NULL for non-quantities
    unit         TEXT,
    value_type   TEXT NOT NULL,             -- quantity|date|entity|categorical|boolean
    qualifiers   TEXT NOT NULL,             -- JSON list of {key,value,inherited}
    modality     TEXT NOT NULL,
    approximate  INTEGER NOT NULL DEFAULT 0,
    confidence   REAL,
    block_ids    TEXT NOT NULL,             -- JSON list; provenance
    -- Literals the cited blocks do not support. Provenance is not accuracy:
    -- a claim can cite the right block and still misread it. JSON list, empty
    -- when the claim's own numbers and dates appear in what it cites.
    grounding_issues TEXT NOT NULL DEFAULT '[]',
    source_modality TEXT NOT NULL DEFAULT 'text',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS facts_by_page ON facts(doc_id, page_index);
CREATE INDEX IF NOT EXISTS facts_by_predicate ON facts(predicate);
"""


def ensure_facts(conn) -> None:
    conn.executescript(FACTS_SCHEMA)


def page_row(conn, doc_id: str, page_index: int):
    return conn.execute(
        "SELECT * FROM pages WHERE doc_id=? AND page_index=?", (doc_id, page_index)
    ).fetchone()


def page_blocks(conn, doc_id: str, page_index: int):
    return conn.execute(
        "SELECT ord, x0, y0, x1, y1, text FROM blocks"
        " WHERE doc_id=? AND page_index=? ORDER BY ord",
        (doc_id, page_index),
    ).fetchall()

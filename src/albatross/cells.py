"""The comparison cell, rendered as a grid.

The system's central claim is that two figures only conflict when they occupy
the same cell - same entity, same predicate, same discriminating qualifiers.
Everywhere else that claim is a `reason_code` the reader has to trust. Here it
is geometry: qualifiers become axes, and agreement, conflict and variance are
positions on a grid rather than assertions.

Two values in one square disagree. Two values in neighbouring squares differ
because of the axis between them, and that axis is the explanation.
"""
from __future__ import annotations

import json
from collections import defaultdict

from . import reconcile, units


def _facts_for(conn, entity_id: str, predicate_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT f.*, d.title FROM facts f JOIN documents d ON d.id = f.doc_id"
        " WHERE f.entity_id = ? AND f.predicate_id = ?",
        (entity_id, predicate_id),
    ).fetchall()
    out = []
    for r in rows:
        f = dict(r)
        f["qualifiers"] = json.loads(r["qualifiers"])
        f["norm"] = units.normalise(r["value"], r["unit"])
        out.append(f)
    return out


def axes(facts: list[dict]) -> list[str]:
    """The qualifier keys worth drawing, most-varied first, at most two."""
    keys = reconcile.discriminating_keys(facts)
    spread: dict[str, set] = defaultdict(set)
    for f in facts:
        for q in f["qualifiers"]:
            k = units.normalise_key(q["key"])
            if k in keys:
                spread[k].add(reconcile._qual_value(k, q["value"]))
    return sorted(spread, key=lambda k: (-len(spread[k]), k))[:2]


def grid(conn, entity_id: str, predicate_id: str) -> dict:
    """Facts arranged over their own qualifier axes."""
    facts = _facts_for(conn, entity_id, predicate_id)
    if not facts:
        return {"axes": [], "rows": [], "cols": [], "cells": {}, "facts": 0}

    ax = axes(facts)
    row_key = ax[0] if ax else None
    col_key = ax[1] if len(ax) > 1 else None

    def position(f):
        got = {units.normalise_key(q["key"]): reconcile._qual_value(
            units.normalise_key(q["key"]), q["value"]) for q in f["qualifiers"]}
        return (got.get(row_key, "—") if row_key else "—",
                got.get(col_key, "—") if col_key else "—")

    cells: dict[str, list[dict]] = defaultdict(list)
    rows, cols = set(), set()
    for f in facts:
        r, c = position(f)
        rows.add(r)
        cols.add(c)
        cells[f"{r}||{c}"].append({
            "id": f["id"], "value": f["value"], "unit": f["unit"],
            "doc": f["title"], "page": f["page_index"] + 1,
            "normalised": f["norm"]["value"] if f["norm"] else None,
            "claim": f["claim_text"],
        })

    # A square holding values that disagree beyond tolerance is the only place
    # a contradiction can live. Everything else is variance across the axes.
    for key, members in cells.items():
        vals = [m["normalised"] for m in members if m["normalised"] is not None]
        conflict = False
        if len(vals) > 1:
            tol = max(
                (f["norm"]["tolerance"] for f in facts if f["norm"]), default=0)
            conflict = (max(vals) - min(vals)) > 2 * tol
        for m in members:
            m["conflict"] = conflict

    return {
        "axes": {"row": row_key, "col": col_key},
        "rows": sorted(rows), "cols": sorted(cols),
        "cells": dict(cells), "facts": len(facts),
    }


def candidates(conn, limit: int = 40) -> list[dict]:
    """Entity/predicate pairs with enough spread to be worth drawing."""
    rows = conn.execute(
        "SELECT f.entity_id, f.predicate_id, COUNT(*) n,"
        " COUNT(DISTINCT f.doc_id) docs,"
        " (SELECT canonical FROM registry r WHERE r.kind='entity'"
        "   AND r.id=f.entity_id) entity,"
        " (SELECT canonical FROM registry r WHERE r.kind='predicate'"
        "   AND r.id=f.predicate_id) predicate"
        " FROM facts f WHERE f.entity_id IS NOT NULL AND f.value IS NOT NULL"
        " GROUP BY f.entity_id, f.predicate_id HAVING n > 1"
        " ORDER BY docs DESC, n DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]

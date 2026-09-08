"""Comparison cells and verdicts.

The single idea the whole system rests on: two facts that disagree numerically
are only a contradiction if they occupy the same *comparison cell* -
(entity, predicate, discriminating qualifier signature). Two revenue figures
with different periods are not in conflict; the qualifier difference IS the
explanation. Nothing here mentions revenue, time or scope by name.

Which qualifiers discriminate is learned from the corpus, not declared: a key
carried by most of a predicate's facts is one that matters for that predicate.
"""
from __future__ import annotations

import json
import uuid
from collections import defaultdict

from . import units

# A qualifier key carried by at least this share of a predicate's facts is
# treated as discriminating for it. Below the bar, its absence is a data gap
# rather than evidence that two facts describe different things.
DISCRIMINATING_SHARE = 0.70

VERDICT_SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    id           TEXT PRIMARY KEY,
    verdict      TEXT NOT NULL,
    reason_code  TEXT NOT NULL,
    fact_a       TEXT NOT NULL,
    fact_b       TEXT NOT NULL,
    entity_id    TEXT,
    predicate_id TEXT,
    qualifier_diff TEXT NOT NULL DEFAULT '{}',
    delta        REAL,
    tolerance    REAL,
    cross_document INTEGER NOT NULL DEFAULT 0,
    trust        TEXT NOT NULL DEFAULT 'high',
    explanation  TEXT
);
CREATE INDEX IF NOT EXISTS verdicts_by_kind ON verdicts(verdict);
"""


def discriminating_keys(facts: list[dict]) -> set[str]:
    """Qualifier keys that actually tell this predicate's facts apart.

    Frequency is the wrong test, and using it was a real bug: `basis` appears
    on a minority of revenue facts, so a frequency rule dropped it - and the
    standalone and consolidated FY24 figures then shared a cell and read as a
    contradiction. What matters is whether a key takes *different values* here.
    A key with one value everywhere explains nothing; a key with several is
    exactly what separates two figures that would otherwise conflict.
    """
    values: dict[str, set[str]] = defaultdict(set)
    carriers: dict[str, int] = defaultdict(int)
    for f in facts:
        seen = set()
        for q in f["qualifiers"]:
            k = units.normalise_key(q["key"])
            values[k].add(_qual_value(k, q["value"]))
            seen.add(k)
        for k in seen:
            carriers[k] += 1

    # Two ways a key earns its place. It takes different values here - or some
    # facts state it and others do not, which is just as discriminating and was
    # the harder case to see: in the IMF's deficit figures only one fact says
    # "FY2023/24" while its neighbour says "estimated to have fallen to", so
    # the key has a single value and a value-spread test alone drops it. The
    # two figures then share a cell and read as a contradiction between
    # consecutive years.
    return {
        k for k in values
        if len(values[k]) >= 2 or (len(facts) > 1 and carriers[k] < len(facts))
    }


def _qual_value(key: str, raw) -> str:
    v = str(raw).strip()
    if key in ("period", "as_of"):
        return units.normalise_period(v) or v.lower()
    return v.lower()


def signature(fact: dict, keys: set[str]) -> tuple:
    """The fact's position in the cell grid, over the discriminating keys only."""
    got = {}
    for q in fact["qualifiers"]:
        k = units.normalise_key(q["key"])
        if k in keys:
            # Period labels reduce to the period they end in, so "FY24",
            # "FY2023-24" and "year ended March 31, 2024" are one position -
            # while "Q3 FY24" stays distinct from its own parent year.
            got[k] = _qual_value(k, q["value"])
    return tuple(sorted(got.items()))


def _load(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT f.*, d.title FROM facts f JOIN documents d ON d.id = f.doc_id"
        " WHERE f.entity_id IS NOT NULL AND f.predicate_id IS NOT NULL"
    ).fetchall()
    out = []
    for r in rows:
        f = dict(r)
        f["qualifiers"] = json.loads(r["qualifiers"])
        f["grounding_issues"] = json.loads(r["grounding_issues"])
        f["norm"] = units.normalise(r["value"], r["unit"])
        out.append(f)
    return out


def _inherited(fact: dict) -> bool:
    """Does this fact's context rest on qualifiers the page never stated?

    grounding.py flags qualifiers whose literals are absent from the cited
    blocks. Those came from the document context card - an inference, not a
    reading. §9: a contradiction resting on inferred context is not a
    contradiction, it is missing information.
    """
    return any("not in cited blocks" in p for p in fact["grounding_issues"])


def compare(a: dict, b: dict, keys: set[str]) -> dict | None:
    """One pairwise verdict, or None if the pair is not worth recording."""
    na, nb = a["norm"], b["norm"]
    if na is None or nb is None or not units.comparable(na, nb):
        return None

    sa, sb = dict(signature(a, keys)), dict(signature(b, keys))
    delta = abs(na["value"] - nb["value"])
    tol = na["tolerance"] + nb["tolerance"]
    agree = delta <= tol
    cross = a["doc_id"] != b["doc_id"]
    base = {"delta": delta, "tolerance": tol, "cross": cross}

    # Two ways signatures can differ, and they mean opposite things. A key both
    # facts state but state differently is an *explanation*. A key only one of
    # them states is *missing information* - we cannot tell whether it would
    # have matched.
    conflicting = {k: (sa[k], sb[k]) for k in set(sa) & set(sb) if sa[k] != sb[k]}
    # Measured against what this predicate *needs*, not merely against what the
    # two facts happen to mention. If `period` discriminates for a predicate and
    # neither fact states one, the two are not in the same cell - they are both
    # under-specified, and the symmetric difference of their own keys is empty
    # so nothing would flag it. That is how two IMF table rows for different
    # years, each stripped of its year, read as a confident contradiction.
    missing = sorted(k for k in keys if k not in sa or k not in sb)

    if conflicting:
        # Facts differing on several axes at once are not reconciled by any one
        # of them; they are simply different measurements.
        if len(conflicting) == 1 and not agree:
            k = next(iter(conflicting))
            return {"verdict": "VARIANT_EXPLAINED_BY",
                    "reason_code": f"QUALIFIER_DIFF:{k}", "diff": conflicting, **base}
        return None

    if missing:
        # Report only the keys one side actually stated. A key neither fact
        # mentions is in `missing` because the predicate needs it, but showing
        # it as [null, null] tells a reader nothing and reads as a bug.
        shown = {k: (sa.get(k), sb.get(k)) for k in missing
                 if sa.get(k) is not None or sb.get(k) is not None}
        if agree:
            return {"verdict": "CORROBORATED",
                    "reason_code": "AGREE_ON_SHARED_QUALIFIERS",
                    "diff": shown, **base}
        # §5: a required qualifier absent on one side is a data gap, not a
        # conflict. Calling it a contradiction asserts something we cannot know.
        return {"verdict": "INSUFFICIENT_CONTEXT",
                "reason_code": f"MISSING_QUALIFIER:{missing[0]}",
                "diff": shown, **base}

    if agree:
        return {"verdict": "CORROBORATED", "reason_code": "SAME_CELL_AGREE",
                "diff": {}, **base}
    if _inherited(a) or _inherited(b):
        return {"verdict": "INSUFFICIENT_CONTEXT",
                "reason_code": "INHERITED_QUALIFIER", "diff": {}, **base}

    # Asserting a contradiction means asserting the two facts measure the same
    # thing. The predicate registry clusters by similarity, and it over-merges
    # (D25) - "finance income" and "total income" can share a cluster. So a
    # contradiction requires the documents to have used the *same words*, not
    # merely similar ones. Every one of the 211 contradictions this produced
    # before the check came from a merged pair with differing predicate strings.
    if a["predicate"].strip().lower() != b["predicate"].strip().lower():
        return {"verdict": "INSUFFICIENT_CONTEXT",
                "reason_code": "PREDICATE_UNCERTAIN",
                "diff": {"predicate": (a["predicate"], b["predicate"])}, **base}

    return {"verdict": "CONTRADICTION", "reason_code": "SAME_CELL_DISAGREE",
            "diff": {}, **base}


def run(conn) -> dict:
    conn.executescript(VERDICT_SCHEMA)
    # Only clear what this pass owns. Judge verdicts (JUDGE:*) cost API calls
    # and cover pairs the comparators cannot reach at all; wiping them here
    # silently deleted them every time reconciliation re-ran.
    conn.execute("DELETE FROM verdicts WHERE reason_code NOT LIKE 'JUDGE:%'")

    facts = _load(conn)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for f in facts:
        groups[(f["entity_id"], f["predicate_id"])].append(f)

    rows, counts = [], defaultdict(int)
    for (ent, pred), group in groups.items():
        keys = discriminating_keys(group)
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                v = compare(a, b, keys)
                if v is None:
                    continue
                counts[v["verdict"]] += 1
                rows.append((
                    str(uuid.uuid4()), v["verdict"], v["reason_code"],
                    a["id"], b["id"], ent, pred, json.dumps(v["diff"]),
                    v["delta"], v["tolerance"], int(v["cross"]),
                    "low" if (a["grounding_issues"] or b["grounding_issues"])
                    else "high", None,
                ))
    conn.executemany(
        "INSERT INTO verdicts (id, verdict, reason_code, fact_a, fact_b,"
        " entity_id, predicate_id, qualifier_diff, delta, tolerance,"
        " cross_document, trust, explanation)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows,
    )
    conn.commit()
    return dict(counts)

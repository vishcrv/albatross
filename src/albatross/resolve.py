"""Entity and predicate registries, resolved incrementally.

Both registries are *derived*, never authored. A surface form is embedded and
assigned to the nearest existing centroid above a threshold, or starts a new
cluster. Nothing here contains a list of known entities or relation types, so a
document about a subject the system has never seen needs no code change - it
just spawns new clusters (docs/decisions.md D4).

Assignment is nearest-centroid, not global re-clustering: document N+1 costs
O(its own mentions), which is what makes the layer incremental (§6).
"""
from __future__ import annotations

import json
import re

import numpy as np

from . import llm

# Calibrated against docs/acceptance.md case #1, which needs "revenue from
# operations" / "revenue from services" / "revenue from contracts with
# customers" to merge, while "Express Parcel revenue" and "revenue from
# services YoY growth" must stay separate. See D25 for the measured spread.
ENTITY_TAU = 0.82
PREDICATE_TAU = 0.78


def unit_class(unit: str | None) -> str:
    """Coarse dimension of a unit string, from the corpus's own wording.

    Deliberately crude and deliberately not a unit ontology: it only has to
    stop things that cannot possibly be the same measurement from merging.
    Embedding similarity cannot see this - "Line haul expenses" and "Line haul
    expenses % of revenue" are near-identical strings describing an amount and
    a ratio.
    """
    if not unit:
        return "unknown"
    u = unit.lower()
    if "%" in u or "percent" in u:
        return "ratio"
    if any(t in u for t in ("₹", "inr", "rupee", "rs", "usd", "$")):
        return "currency"
    if any(t in u for t in ("ton", "tonne", " kg", "kilogram")):
        return "mass"
    return "other"


_DIGITS = re.compile(r"\d+")


def _may_merge(a_units: set[str], b_units: set[str], a: str, b: str) -> bool:
    """Hard gates applied before similarity is even consulted.

    Compared against the cluster's canonical form, never against the union of
    everything it has absorbed: a union grows until it intersects anything, so
    the gate would erode exactly as the cluster gets big enough to matter.
    """
    # Different numerals mean different things. Embeddings barely encode
    # digits, so "Activity 8" and "Activity 9" are near-identical vectors.
    if set(_DIGITS.findall(a)) != set(_DIGITS.findall(b)):
        return False
    # Only a genuinely absent unit is permissive. "other" is a real class: it
    # is where bare magnitude words land ("crore", "million"), which are used
    # for both money and counts, and calling those currency would be guessing.
    known_a = a_units - {"unknown"}
    known_b = b_units - {"unknown"}
    if known_a and known_b and not (known_a & known_b):
        return False
    return True

REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS registry (
    kind        TEXT NOT NULL,             -- entity | predicate
    id          TEXT NOT NULL,
    canonical   TEXT NOT NULL,
    surface_forms TEXT NOT NULL,           -- JSON list
    centroid    TEXT NOT NULL,             -- JSON list of floats
    n           INTEGER NOT NULL,
    PRIMARY KEY (kind, id)
);
ALTER_FACTS_ENTITY = 0;
"""


def _ensure(conn):
    conn.executescript(REGISTRY_SCHEMA.replace("ALTER_FACTS_ENTITY = 0;", ""))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)")}
    if "entity_id" not in cols:
        conn.execute("ALTER TABLE facts ADD COLUMN entity_id TEXT")
    if "predicate_id" not in cols:
        conn.execute("ALTER TABLE facts ADD COLUMN predicate_id TEXT")
    conn.commit()


def _norm(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.where(n == 0, 1, n)


def build(conn, kind: str, column: str, tau: float) -> dict[str, str]:
    """Cluster the distinct surface forms in `column`. Returns form -> cluster id.

    Forms are processed most-frequent-first so the commonest wording becomes the
    canonical label, rather than whichever happened to be seen first.
    """
    _ensure(conn)
    rows = conn.execute(
        f"SELECT {column} AS form, COUNT(*) n FROM facts"
        f" WHERE {column} != '' GROUP BY {column} ORDER BY n DESC"
    ).fetchall()
    forms = [r["form"] for r in rows]
    if not forms:
        return {}

    # Units observed per surface form - the data's own evidence about what
    # kind of measurement each predicate is.
    units: dict[str, set[str]] = {f: set() for f in forms}
    for u_row in conn.execute(
        f"SELECT {column} AS form, unit FROM facts WHERE {column} != ''"
    ):
        if u_row["form"] in units:
            units[u_row["form"]].add(unit_class(u_row["unit"]))

    vecs = _norm(np.array(llm.embed_many(forms), dtype=np.float32))

    centroids: list[np.ndarray] = []       # running means, normalised
    members: list[list[str]] = []
    counts: list[int] = []
    assign: dict[str, str] = {}

    for form, row, v in zip(forms, rows, vecs):
        if centroids:
            sims = np.stack(centroids) @ v
            order = np.argsort(-sims)
            best = next(
                (int(i) for i in order
                 if _may_merge(units[form], units[members[int(i)][0]],
                               form, members[int(i)][0])),
                None,
            )
            if best is not None and sims[best] >= tau:
                members[best].append(form)
                counts[best] += row["n"]
                # Running mean weighted by how many facts each form carries.
                c = centroids[best] * (counts[best] - row["n"]) + v * row["n"]
                centroids[best] = c / np.linalg.norm(c)
                assign[form] = f"{kind[:3]}_{best:04d}"
                continue
        centroids.append(v)
        members.append([form])
        counts.append(row["n"])

        assign[form] = f"{kind[:3]}_{len(centroids)-1:04d}"

    conn.execute("DELETE FROM registry WHERE kind=?", (kind,))
    conn.executemany(
        "INSERT INTO registry (kind, id, canonical, surface_forms, centroid, n)"
        " VALUES (?,?,?,?,?,?)",
        [(kind, f"{kind[:3]}_{i:04d}", m[0], json.dumps(m),
          json.dumps(c.tolist()), n)
         for i, (m, c, n) in enumerate(zip(members, centroids, counts))],
    )
    col = "entity_id" if kind == "entity" else "predicate_id"
    conn.executemany(
        f"UPDATE facts SET {col}=? WHERE {column}=?",
        [(cid, form) for form, cid in assign.items()],
    )
    conn.commit()
    return assign


def resolve_all(conn):
    e = build(conn, "entity", "subject", ENTITY_TAU)
    p = build(conn, "predicate", "predicate", PREDICATE_TAU)
    return {"entities": len(set(e.values())), "predicates": len(set(p.values()))}

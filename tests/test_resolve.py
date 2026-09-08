"""Registry clustering: the merges that must happen, and the ones that must not.

Thresholds and gates are the least stable part of this system, so the cases
that matter are pinned here. Each was observed failing during development.

Runs against the built albatross.db; skips if it is absent.
"""
import sys, pathlib, json, sqlite3
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from albatross.resolve import unit_class, _may_merge

DB = pathlib.Path("albatross.db")


def _cluster(conn, form, kind):
    for r in conn.execute("SELECT surface_forms FROM registry WHERE kind=?", (kind,)):
        forms = json.loads(r[0])
        if form in forms:
            return forms
    return None


MUST_MERGE = [
    ("Delhivery Limited", "Delhivery", "entity"),
    ("Sahil Barua", "Mr. Sahil Barua", "entity"),
]
MUST_SPLIT = [
    # An amount and a ratio of that amount. Near-identical strings.
    ("Line haul expenses", "Line haul expenses % of revenue", "predicate"),
    # Mass and money.
    ("PTL freight tonnage", "PTL freight revenue", "predicate"),
    # A segment against the total. Merging these fabricates a contradiction:
    # segment facts carry no `segment` qualifier, so they would share a
    # comparison cell with the total and disagree by construction.
    ("Express Parcel revenue", "Revenue from services", "predicate"),
    # Embeddings barely encode digits.
    ("Activity 8", "Activity 9", "entity"),
]


def test_registry_merges_and_splits():
    if not DB.exists():
        print("SKIP (no albatross.db)"); return
    conn = sqlite3.connect(DB)
    try:
        for a, b, kind in MUST_MERGE:
            c = _cluster(conn, a, kind)
            assert c and b in c, f"{a!r} and {b!r} should share a cluster; got {c}"
        for a, b, kind in MUST_SPLIT:
            c = _cluster(conn, a, kind)
            assert c is None or b not in c, f"{a!r} and {b!r} must not merge; got {c}"
    finally:
        conn.close()


def test_case_1_shares_entity_and_predicate():
    """docs/acceptance.md #1 - the corroboration cannot happen otherwise."""
    if not DB.exists():
        print("SKIP (no albatross.db)"); return
    conn = sqlite3.connect(DB)
    try:
        ids = {v: (p, e) for v, p, e in conn.execute(
            "SELECT value, predicate_id, entity_id FROM facts"
            " WHERE value IN (81415.38, 8142)")}
        assert 81415.38 in ids and 8142.0 in ids, f"facts missing: {list(ids)}"
        assert ids[81415.38] == ids[8142.0], (
            f"AR and deck figures must share a cell: {ids[81415.38]} vs {ids[8142.0]}")
    finally:
        conn.close()


def test_unit_gate_semantics():
    assert unit_class("%") == "ratio"
    assert unit_class("₹ Cr") == "currency"
    assert unit_class("'000 Tons") == "mass"
    assert unit_class("crore") == "other"     # magnitude word, not a currency
    assert unit_class(None) == "unknown"
    # Absent units stay permissive; that is what keeps entity merging working.
    assert _may_merge({"unknown"}, {"unknown"}, "Sahil Barua", "Mr. Sahil Barua")
    assert not _may_merge({"mass"}, {"other"}, "tonnage", "revenue")
    assert not _may_merge({"unknown"}, {"unknown"}, "Activity 8", "Activity 9")


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f(); print("PASS", n)
    print("\nregistry behaviour pinned")

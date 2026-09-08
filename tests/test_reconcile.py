"""Verdicts for the acceptance cases, and the normalisation they depend on.

Every case here was wrong at some point during development; each assertion
corresponds to a specific defect (see docs/decisions.md D26).
"""
import sys, pathlib, sqlite3
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from albatross import units, reconcile

DB = pathlib.Path("albatross.db")


def _verdict_between(conn, va, vb):
    row = conn.execute(
        "SELECT v.verdict, v.reason_code, v.delta, v.tolerance"
        " FROM verdicts v JOIN facts a ON a.id=v.fact_a JOIN facts b ON b.id=v.fact_b"
        " WHERE (a.value=? AND b.value=?) OR (a.value=? AND b.value=?) LIMIT 1",
        (va, vb, vb, va),
    ).fetchone()
    return row


def test_case_1_corroborates_across_documents():
    """₹81,415.38 million and 8,142 Cr are the same figure, differently scaled."""
    if not DB.exists():
        print("SKIP"); return
    conn = sqlite3.connect(DB)
    try:
        r = _verdict_between(conn, 81415.38, 8142.0)
        assert r is not None, "no verdict linking the AR and deck revenue figures"
        assert r[0] == "CORROBORATED", f"got {r[0]} / {r[1]}"
        assert r[2] <= r[3], f"delta {r[2]:,.0f} exceeded tolerance {r[3]:,.0f}"
    finally:
        conn.close()


def test_case_2a_is_a_variant_not_a_contradiction():
    """Standalone vs consolidated, same period: explained by one qualifier."""
    if not DB.exists():
        print("SKIP"); return
    conn = sqlite3.connect(DB)
    try:
        r = _verdict_between(conn, 74540.82, 81415.38)
        assert r is not None, "no verdict between standalone and consolidated"
        assert r[0] == "VARIANT_EXPLAINED_BY", f"got {r[0]} / {r[1]}"
        assert r[1] == "QUALIFIER_DIFF:basis", r[1]
    finally:
        conn.close()


def test_rounding_is_not_disagreement():
    """Tolerance comes from printed precision, not a fixed epsilon."""
    a = units.normalise(81415.38, "₹ million")
    b = units.normalise(8142, "Cr")
    assert abs(a["value"] - b["value"]) <= a["tolerance"] + b["tolerance"]
    # A genuinely different figure must still fail.
    c = units.normalise(5689, "₹ Cr")
    assert abs(a["value"] - c["value"]) > a["tolerance"] + c["tolerance"]


def test_plural_magnitudes_scale():
    """'Indian Rupees in millions' silently lost a factor of 10^6."""
    assert units.normalise(4724.02, "Indian Rupees in millions")["value"] == 4724.02e6


def test_quarter_is_not_its_parent_year():
    """Otherwise a quarterly figure contradicts the annual one."""
    assert units.normalise_period("Q3 FY24") != units.normalise_period("FY24")
    assert units.normalise_period("FY2023-24") == units.normalise_period("FY24")


def test_discriminating_keys_use_value_spread_not_frequency():
    """A frequency rule dropped `basis` and turned case 2a into a contradiction."""
    facts = [
        {"qualifiers": [{"key": "period", "value": "FY24"},
                        {"key": "basis", "value": "standalone"}]},
        {"qualifiers": [{"key": "period", "value": "FY24"},
                        {"key": "basis", "value": "consolidated"}]},
        {"qualifiers": [{"key": "period", "value": "FY24"}]},
        {"qualifiers": [{"key": "period", "value": "FY24"}]},
    ]
    keys = reconcile.discriminating_keys(facts)
    assert "basis" in keys, "basis distinguishes these facts and must be kept"
    assert "period" not in keys, "one value everywhere explains nothing"


def _fact(value, quals, doc="d1", pred="revenue from operations"):
    return {"id": f"f{value}", "doc_id": doc, "predicate": pred, "value": value,
            "unit": "INR million", "qualifiers": [{"key": k, "value": v}
                                                  for k, v in quals.items()],
            "grounding_issues": [], "norm": units.normalise(value, "INR million")}


def test_contradiction_fires_when_one_genuinely_exists():
    """The corpus contains no contradiction (see D29/D33), so prove the
    machinery would report one. Same entity, same predicate wording, identical
    discriminating qualifiers, values far outside tolerance."""
    a = _fact(81415.38, {"period": "FY24", "basis": "consolidated"}, doc="d1")
    b = _fact(72253.01, {"period": "FY24", "basis": "consolidated"}, doc="d2")
    keys = reconcile.discriminating_keys([a, b])
    v = reconcile.compare(a, b, keys)
    assert v is not None and v["verdict"] == "CONTRADICTION", v
    assert v["reason_code"] == "SAME_CELL_DISAGREE", v


def test_contradiction_is_withheld_without_predicate_certainty():
    """Same numbers, but the documents used different words for the metric."""
    a = _fact(81415.38, {"period": "FY24"}, doc="d1", pred="total income")
    b = _fact(72253.01, {"period": "FY24"}, doc="d2", pred="finance income")
    v = reconcile.compare(a, b, reconcile.discriminating_keys([a, b]))
    assert v["verdict"] == "INSUFFICIENT_CONTEXT", v
    assert v["reason_code"] == "PREDICATE_UNCERTAIN", v


def test_fiscal_and_calendar_quarters_do_not_collide():
    """The Economic Survey's Q2 FY25 is not the IMF's 2025Q2."""
    a = _fact(1.2, {"period": "Q2 FY25"}, doc="d1", pred="current account deficit")
    b = _fact(0.2, {"period": "2025Q2"}, doc="d2", pred="current account deficit")
    v = reconcile.compare(a, b, reconcile.discriminating_keys([a, b]))
    assert v["verdict"] == "VARIANT_EXPLAINED_BY", v
    assert v["reason_code"] == "QUALIFIER_DIFF:period", v


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f(); print("PASS", n)
    print("\nreconciliation pinned")

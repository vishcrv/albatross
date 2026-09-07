"""Incremental ingestion: re-adding known pages must be a no-op.

This is the property §6's scaling argument rests on, and a stated brownie
point in the brief ("adding new documents incrementally, without rebuilding").
"""
import sys, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from albatross.cli import ingest

PROSPECTUS = pathlib.Path("data/delhivery/01-delhivery-prospectus-2022-excerpt.pdf")


def test_reingesting_the_same_pages_adds_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        db = str(pathlib.Path(tmp) / "t.db")
        first = ingest(db, PROSPECTUS, 26, 30)
        again = ingest(db, PROSPECTUS, 26, 30)
        assert first["pages_added"] == 5
        assert again["pages_added"] == 0
        assert again["pages_skipped"] == 5


def test_overlapping_range_adds_only_the_new_pages():
    with tempfile.TemporaryDirectory() as tmp:
        db = str(pathlib.Path(tmp) / "t.db")
        ingest(db, PROSPECTUS, 26, 30)
        r = ingest(db, PROSPECTUS, 24, 30)
        assert r["pages_added"] == 2, r
        assert r["pages_skipped"] == 5, r


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn(); print("PASS", name)
    print("\nincremental ingestion holds")

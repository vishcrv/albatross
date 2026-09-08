"""API smoke tests: every route answers, and evidence really is a PNG.

Runs against the built albatross.db; skips if it is absent.
"""
import sys, pathlib, sqlite3
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

DB = pathlib.Path("albatross.db")


def _client():
    from fastapi.testclient import TestClient
    from albatross.api import app
    return TestClient(app)


def test_pages_render():
    if not DB.exists():
        print("SKIP"); return
    c = _client()
    for path in ("/", "/verdicts", "/verdicts?verdict=CORROBORATED",
                 "/facts", "/facts?q=revenue", "/entities?q=Delhivery"):
        r = c.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        # An unrendered template variable means the page silently lost content.
        assert "{{" not in r.text, f"{path} has unrendered template markers"


def test_evidence_is_a_real_png():
    if not DB.exists():
        print("SKIP"); return
    conn = sqlite3.connect(DB)
    fid = conn.execute("SELECT id FROM facts WHERE value IS NOT NULL"
                       " LIMIT 1").fetchone()[0]
    conn.close()
    r = _client().get(f"/evidence/{fid}.png")
    assert r.status_code == 200, r.status_code
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG payload"
    assert len(r.content) > 2000, "crop suspiciously small"


def test_unknown_ids_404_rather_than_500():
    if not DB.exists():
        print("SKIP"); return
    c = _client()
    assert c.get("/evidence/nope.png").status_code == 404
    assert c.get("/documents/nope").status_code == 404


def test_reconciliation_json_carries_evidence_links():
    if not DB.exists():
        print("SKIP"); return
    rows = _client().get("/api/reconciliations?limit=3").json()
    assert rows, "no reconciliations returned"
    for row in rows:
        assert {"verdict", "reason_code", "cell", "facts"} <= set(row)
        assert len(row["facts"]) == 2
        for f in row["facts"]:
            assert f["evidence"].startswith("/evidence/")


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f(); print("PASS", n)
    print("\napi smoke passes")

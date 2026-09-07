"""Regression tests for the two extraction failures in docs/acceptance.md #4.

Fixtures name documents and pages; src/ never may. Ground truth for the DINs
comes from the Annual Report's prose, which states them inline and is
unaffected by the table-geometry bug.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from albatross.pdf import read_pages

PROSPECTUS = "data/delhivery/01-delhivery-prospectus-2022-excerpt.pdf"
ANNUAL_REPORT = "data/delhivery/02-delhivery-annual-report-fy24-excerpt.pdf"

# name -> DIN, per the Annual Report prose (physical pages 24 and 40).
BOARD = {
    "Deepak Kapoor": "00162957",
    "Sahil Barua": "05131571",
    "Sandeep Kumar Barasia": "01432123",
    "Kapil Bharati": "02227607",
    "Donald Francis Colleran": "09431299",
    "Munish Ravinder Varma": "02442753",
    "Suvir Suren Sujan": "01173669",
}


def test_board_table_pairs_each_director_with_the_right_din():
    """pdftotext -layout offsets this column by two rows, silently.

    Name and DIN are separate blocks (separate table columns), so the test is
    that the pairing is *recoverable from geometry* - the two blocks overlap
    vertically and nothing else intrudes on that row. That is exactly what
    layout_text() hands the extractor.
    """
    page = next(read_pages(PROSPECTUS, 30, 30))

    def row_overlap(a, b):
        return min(a.bbox[3], b.bbox[3]) - max(a.bbox[1], b.bbox[1])

    for name, din in BOARD.items():
        name_b = next((b for b in page.blocks if name in b.text), None)
        assert name_b is not None, f"{name} not found on the page"
        aligned = [
            b for b in page.blocks
            if b.ord != name_b.ord and row_overlap(name_b, b) > 0
            and any(c.isdigit() for c in b.text)
        ]
        dins = [b.text for b in aligned if din in b.text]
        assert dins, (
            f"{name}: DIN {din} not vertically aligned with the name block; "
            f"aligned blocks were {[b.text[:40] for b in aligned]}"
        )


def test_layout_text_carries_block_ids_and_coordinates():
    page = next(read_pages(PROSPECTUS, 30, 30))
    line = next(l for l in page.layout_text().splitlines() if "Sahil Barua" in l)
    assert line.startswith("[b"), line
    assert page.block(0) is not None


def test_multi_column_prose_is_not_spliced_across_columns():
    """Both revenue sentences must survive intact, not interleaved."""
    page = next(read_pages(ANNUAL_REPORT, 22, 22))
    text = page.text
    for phrase in (
        "on standalone basis for FY24",
        "on consolidated basis for",
    ):
        assert phrase in text, f"missing: {phrase}"
    # The splice signature: a sentence continuing into an unrelated column.
    assert "standalone basis for FY24 such as fast-moving" not in text
    assert "algorithms andy The revenue" not in text


def test_currency_symbol_survives():
    page = next(read_pages(ANNUAL_REPORT, 22, 22))
    assert "₹" in page.text, "rupee sign dropped in extraction"


def test_page_hash_is_stable_across_reads():
    a = next(read_pages(PROSPECTUS, 30, 30))
    b = next(read_pages(PROSPECTUS, 30, 30))
    assert a.page_hash == b.page_hash


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
    print("\nall extraction regressions pass")

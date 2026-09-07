"""Page-atomic PDF reading: column-ordered blocks with bounding boxes.

Flat text extraction is not usable input for this corpus - see
docs/acceptance.md case #4. Two reproducible failures motivated everything
here: a borderless table whose columns pair up only by geometry, and a
multi-column page whose columns splice into sentences that never existed.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

import pymupdf

# Blocks whose left edge differs by less than this fraction of page width are
# the same column. Indented bullets sit ~11pt right of their column start;
# adjacent columns are ~250pt apart, so anything in between works.
COLUMN_GAP_RATIO = 0.05


@dataclass(frozen=True)
class Block:
    ord: int
    bbox: tuple[float, float, float, float]
    text: str


@dataclass(frozen=True)
class Page:
    index: int          # 0-based physical page
    labels: list[str]   # printed page label(s); a spread carries more than one
    width: float
    height: float
    blocks: list[Block]
    page_hash: str

    @property
    def text(self) -> str:
        """Reading-order text: a hint for humans and for coreference across
        page boundaries, not the extractor's input - see layout_text()."""
        return "\n".join(b.text for b in self.blocks)

    def layout_text(self) -> str:
        """What the claim extractor actually sees.

        Every block is tagged with its id and integer bbox. No single reading
        order serves both multi-column prose (column-major) and borderless
        tables (row-major), and on this corpus the two are not separable by
        geometry alone - prose columns share y0 exactly as table cells do. So
        we do not choose: the model gets the coordinates, resolves layout
        itself, and cites block ids back, which is also what grounds the claim.
        """
        return "\n".join(
            "[b{} {},{} {},{}] {}".format(
                b.ord, *(round(v) for v in b.bbox), b.text.replace("\n", " / ")
            )
            for b in self.blocks
        )

    def block(self, ord_: int) -> Block | None:
        return next((b for b in self.blocks if b.ord == ord_), None)


def _column_bands(x0s: list[float], page_width: float) -> list[float]:
    """Left edges of detected column bands, ascending."""
    threshold = page_width * COLUMN_GAP_RATIO
    bands: list[float] = []
    for x in sorted(x0s):
        if not bands or x - bands[-1] > threshold:
            bands.append(x)
    return bands


def _band_of(x0: float, bands: list[float]) -> int:
    """Index of the rightmost band starting at or before x0."""
    return max(i for i, b in enumerate(bands) if x0 >= b - 0.01)


def _printed_labels(page: pymupdf.Page, raw_blocks: list[dict]) -> list[str]:
    """Printed page label(s). A two-page spread legitimately has two."""
    declared = page.get_label()
    if declared:
        return [declared]
    # Fall back to spans in the top/bottom 8% bands that are *nothing but* a
    # plausible page number. Scraping every digit out of a running footer
    # ("Annual Report 2023-24 43 42 Delhivery Limited") yields years and
    # fragments, not labels.
    band = page.rect.height * 0.08
    found: list[str] = []
    for b in raw_blocks:
        for line in b["lines"]:
            for s in line["spans"]:
                y0, y1 = s["bbox"][1], s["bbox"][3]
                if y0 > band and y1 < page.rect.height - band:
                    continue
                text = s["text"].strip()
                if re.fullmatch(r"\d{1,4}", text) and int(text) < 2000:
                    found.append(text)
    return found


def _split_block_by_column(block: dict, page_width: float):
    """Yield (bbox, text) for each column band within one PyMuPDF block.

    PyMuPDF groups a borderless table's cells into blocks that run diagonally
    across it - one block can hold a director's DIN, that director's address,
    and the *next* director's name. Emitting that as a unit reproduces exactly
    the misattribution this module exists to prevent (acceptance.md #4).
    Prose blocks have all their lines in one band and pass through unchanged.
    """
    rows = []
    for line in block["lines"]:
        text = "".join(s["text"] for s in line["spans"]).strip()
        if text:
            rows.append((line["bbox"], text))
    if not rows:
        return

    bands = _column_bands([bb[0] for bb, _ in rows], page_width)
    grouped: dict[int, list] = {}
    for bb, text in rows:
        grouped.setdefault(_band_of(bb[0], bands), []).append((bb, text))

    for band in sorted(grouped):
        items = sorted(grouped[band], key=lambda it: it[0][1])
        bbox = (
            min(bb[0] for bb, _ in items), min(bb[1] for bb, _ in items),
            max(bb[2] for bb, _ in items), max(bb[3] for bb, _ in items),
        )
        yield bbox, "\n".join(t for _, t in items)


def read_page(page: pymupdf.Page) -> Page:
    raw = [b for b in page.get_text("dict")["blocks"] if b.get("type") == 0]
    lines = []
    for b in raw:
        lines.extend(_split_block_by_column(b, page.rect.width))

    bands = _column_bands([bb[0] for bb, _ in lines], page.rect.width) or [0.0]
    ordered = sorted(lines, key=lambda it: (_band_of(it[0][0], bands), it[0][1]))
    blocks = [Block(i, tuple(bb), t) for i, (bb, t) in enumerate(ordered)]

    body = "\n".join(b.text for b in blocks)
    return Page(
        index=page.number,
        labels=_printed_labels(page, raw),
        width=page.rect.width,
        height=page.rect.height,
        blocks=blocks,
        page_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
    )


def read_pages(path: str, first: int | None = None, last: int | None = None):
    """Yield Pages for 1-based inclusive physical page range [first, last]."""
    with pymupdf.open(path) as doc:
        lo = (first - 1) if first else 0
        hi = (last - 1) if last else doc.page_count - 1
        for i in range(max(0, lo), min(doc.page_count - 1, hi) + 1):
            yield read_page(doc[i])

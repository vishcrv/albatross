"""Page-region crops: the bit that makes a verdict checkable by a human.

A verdict nobody can verify is an opinion. Every fact carries the block ids it
came from, and every block carries a bounding box, so the evidence for a claim
is a picture of the exact region of the exact page it was read from - with the
cited blocks outlined.
"""
from __future__ import annotations

import io
import json

import pymupdf

PAD = 18          # points of context around the cited region
MIN_HEIGHT = 90   # a one-line crop is unreadable without surrounding context
DPI = 144


def crop(conn, fact_id: str) -> bytes:
    """PNG of the page region a fact was extracted from, cited blocks outlined."""
    row = conn.execute(
        "SELECT f.doc_id, f.page_index, f.block_ids, d.path"
        " FROM facts f JOIN documents d ON d.id = f.doc_id WHERE f.id = ?",
        (fact_id,),
    ).fetchone()
    if row is None:
        raise KeyError(fact_id)

    ids = json.loads(row["block_ids"])
    boxes = [
        (r["x0"], r["y0"], r["x1"], r["y1"])
        for r in conn.execute(
            "SELECT x0, y0, x1, y1 FROM blocks"
            " WHERE doc_id=? AND page_index=? AND ord IN"
            f" ({','.join('?' * len(ids))})",
            (row["doc_id"], row["page_index"], *ids),
        )
    ] if ids else []

    with pymupdf.open(row["path"]) as doc:
        page = doc[row["page_index"]]
        if boxes:
            clip = pymupdf.Rect(
                min(b[0] for b in boxes) - PAD, min(b[1] for b in boxes) - PAD,
                max(b[2] for b in boxes) + PAD, max(b[3] for b in boxes) + PAD,
            )
            if clip.height < MIN_HEIGHT:
                grow = (MIN_HEIGHT - clip.height) / 2
                clip.y0 -= grow
                clip.y1 += grow
            clip &= page.rect          # never ask for pixels off the page
        else:
            clip = page.rect

        # Outline rather than fill: a highlight that obscures the numbers it is
        # pointing at defeats the purpose.
        for b in boxes:
            page.draw_rect(pymupdf.Rect(*b), color=(0.85, 0.3, 0.1), width=1.2)

        pix = page.get_pixmap(clip=clip, dpi=DPI)
        buf = io.BytesIO(pix.tobytes("png"))
    return buf.getvalue()

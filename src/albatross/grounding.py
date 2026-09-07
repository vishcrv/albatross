"""Literal-level grounding check.

Citing a block proves provenance, not accuracy. Observed on the annual report's
cessation page: a claim correctly cited the block reading "resigned from the
Board with effect from August 24, 2023" and recorded the date as August 04,
2023 - the date printed three times elsewhere on the same page. The claim was
grounded and wrong.

So every literal a claim asserts - its number, and any date-shaped qualifier -
is checked against the text of the blocks it cites. Nothing here knows what a
revenue or a director is; it compares strings and numbers.
"""
from __future__ import annotations

import re

MONTHS = ("january february march april may june july august september "
          "october november december").split()
DATE_RE = re.compile(
    r"\b(" + "|".join(MONTHS) + r")\s+\d{1,2},?\s+\d{4}\b"
    r"|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)


def _numbers_in(text: str) -> set[str]:
    """Numeric literals, comma-stripped. 10 significant digits: %.6g collapses
    74,540.82 and 74,540.83 onto the same string, which would hide exactly the
    kind of near-miss this check exists to catch."""
    out = set()
    for tok in re.findall(r"\d[\d,]*\.?\d*", text):
        try:
            out.add(f"{float(tok.replace(',', '')):.10g}")
        except ValueError:
            continue
    # Accounting notation: a figure in parentheses is negative. Without this
    # the check flags every loss line in the corpus as unsupported.
    for tok in re.findall(r"\(\s*(\d[\d,]*\.?\d*)\s*[^)\d]{0,8}\)", text):
        try:
            out.add(f"{-float(tok.replace(',', '')):.10g}")
        except ValueError:
            continue
    return out


def _dates_in(text: str) -> set[str]:
    return {re.sub(r"[\s,]+", " ", m.group(0)).strip().lower()
            for m in DATE_RE.finditer(text)}


def check(claim: dict, cited_text: str) -> list[str]:
    """Return a list of literals the cited blocks do not support. Empty is good."""
    problems: list[str] = []

    value = claim.get("value")
    if value is not None:
        if f"{float(value):.10g}" not in _numbers_in(cited_text):
            problems.append(f"value {value} not in cited blocks")

    supported = _dates_in(cited_text)
    if supported:
        for q in claim.get("qualifiers", []):
            for d in _dates_in(str(q.get("value", ""))):
                if d not in supported:
                    problems.append(f"{q['key']}={q['value']} not in cited blocks")
    return problems


def demo():
    blocks = ("(DIN: 01173669), resigned from the Board with effect "
              "from August 24, 2023, on account of pre-occupation.")
    bad = {"value": None, "qualifiers": [{"key": "effective_date",
                                          "value": "August 04, 2023"}]}
    good = {"value": None, "qualifiers": [{"key": "effective_date",
                                           "value": "August 24, 2023"}]}
    assert check(bad, blocks), "should catch the wrong date"
    assert not check(good, blocks), "should accept the right date"

    nums = "Revenue from Operations 74,540.82 66,586.61 81,415.38"
    assert not check({"value": 74540.82, "qualifiers": []}, nums)
    assert check({"value": 74540.83, "qualifiers": []}, nums)
    # A figure restated in different units is not "unsupported" by accident:
    # the check only fires on the literal, so unit conversion stays downstream.
    assert check({"value": 8142, "qualifiers": []}, nums)
    # Accounting notation.
    assert not check({"value": -8987.45, "qualifiers": []}, "Loss before tax (8,987.45)")
    assert not check({"value": -1008, "qualifiers": []}, "PAT loss Rs. (1,008 Cr)")
    assert not check({"value": -6.3, "qualifiers": []}, "EBITDA margin (6.3%)")
    print("grounding checks ok")


if __name__ == "__main__":
    demo()

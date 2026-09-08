"""Normalising values, units, and qualifier labels so they can be compared.

No numeric comparison in this system means anything before this module runs:
the corpus states the same figure as "₹81,415.38 million" and "8,142 Cr".

Tolerance is derived from *stated precision*, not from a fixed epsilon. A
figure printed as "8,142 Cr" asserts nothing below one crore, so comparing it
against a figure printed to two decimal places of a million must not call the
difference a disagreement. This is the whole reason rounding does not become a
contradiction.
"""
from __future__ import annotations

import re

# Magnitude words. Indian and international scales both appear in this corpus,
# and a document may mix them on one page.
MAGNITUDES = {
    "thousand": 1e3, "k": 1e3, "'000": 1e3, "000": 1e3,
    "lakh": 1e5, "lac": 1e5,
    "million": 1e6, "mn": 1e6, "mio": 1e6, "m": 1e6,
    "crore": 1e7, "cr": 1e7,
    "billion": 1e9, "bn": 1e9,
    "trillion": 1e12,
}
CURRENCIES = {
    "₹": "INR", "inr": "INR", "rs": "INR", "rupee": "INR", "rupees": "INR",
    "$": "USD", "usd": "USD", "us$": "USD",
    "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP",
}
_TOKEN = re.compile(r"[^\W\d_]+|[₹$€£]|'?\d+", re.UNICODE)


def parse_unit(unit: str | None) -> dict:
    """Split a unit string into currency, magnitude and dimension."""
    if not unit:
        return {"currency": None, "magnitude": 1.0, "dimension": "unknown",
                "residual": ""}
    u = unit.strip().lower()
    if "%" in u or "percent" in u:
        return {"currency": None, "magnitude": 1.0, "dimension": "ratio",
                "residual": "%"}

    currency, magnitude, residual = None, 1.0, []
    for tok in _TOKEN.findall(u):
        # Plurals matter: "Indian Rupees in millions" missed the magnitude
        # table entirely and left the value short by a factor of 10^6, which
        # surfaced as a confident cross-document contradiction.
        singular = tok[:-1] if tok.endswith("s") and len(tok) > 2 else tok
        if tok in CURRENCIES or singular in CURRENCIES:
            currency = CURRENCIES.get(tok) or CURRENCIES[singular]
        elif magnitude == 1.0 and (tok in MAGNITUDES or singular in MAGNITUDES):
            magnitude = MAGNITUDES.get(tok) or MAGNITUDES[singular]
        elif tok in ("in", "of", "per", "s"):
            continue
        else:
            residual.append(tok)

    residual_s = " ".join(residual)
    if currency:
        dimension = "currency"
    elif any(t in residual_s for t in ("ton", "tonne", "kg", "kilogram")):
        dimension = "mass"
    elif residual_s:
        dimension = "count"          # shipments, employees, shares, PIN codes
    else:
        dimension = "magnitude"      # a bare "Cr" - scale without a substance
    return {"currency": currency, "magnitude": magnitude,
            "dimension": dimension, "residual": residual_s}


def normalise(value: float | None, unit: str | None) -> dict | None:
    """Value in base units, with the tolerance its own printed precision implies."""
    if value is None:
        return None
    u = parse_unit(unit)
    scaled = value * u["magnitude"]
    return {"value": scaled, "tolerance": precision_tolerance(value, u["magnitude"]),
            **u}


def precision_tolerance(value: float, magnitude: float = 1.0) -> float:
    """Half the place value of the last digit the source actually printed.

    "8,142 Cr" resolves nothing finer than a crore, so its true value lies
    within ±0.5 crore. "81,415.38 million" is printed to 0.01 million. Compare
    the two and the tolerance is the sum - which is exactly why these two
    corroborate instead of disagreeing by 4.6 million rupees.
    """
    if value == 0:
        return 0.5 * magnitude
    text = f"{abs(value):.10g}"
    if "." in text:
        step = 10 ** -len(text.split(".", 1)[1])
    else:
        # An integer that is a round multiple of 10 asserts only the digits it
        # shows: 8,140 claims less precision than 8,142.
        step = 1.0
        while text.endswith("0") and len(text) > 1:
            step *= 10
            text = text[:-1]
    return 0.5 * step * magnitude


def comparable(a: dict, b: dict) -> bool:
    """Are two normalised values even the same kind of thing?"""
    if a["dimension"] == "unknown" or b["dimension"] == "unknown":
        return True                              # unknown is not a claim
    soft = {"magnitude", "currency"}             # a bare "Cr" beside "₹ Cr"
    if {a["dimension"], b["dimension"]} <= soft:
        pass
    elif a["dimension"] != b["dimension"]:
        return False
    if a["currency"] and b["currency"] and a["currency"] != b["currency"]:
        return False
    return True


# "FY2023-24" and "2023-24" span two calendar years and end in the later one.
# \b is wrong here: "FY2023" has no word boundary between the Y and the 2,
# so a \b-anchored pattern silently matches nothing on the commonest form.
SPAN = re.compile(r"(?<!\d)(\d{4})\s*[-–/]\s*(\d{2})(?!\d)")
YEAR = re.compile(r"(?<!\d)(\d{4})(?!\d)|fy\s*['’]?(\d{2})(?!\d)", re.IGNORECASE)


def normalise_period(text: str) -> str | None:
    """Reduce a period label to the year it ends in.

    Deliberately does NOT assert month boundaries. "FY24" means April-March in
    India and something else elsewhere; inferring a date range from a label is
    a guess this system does not need to make. The terminal year is enough to
    tell two periods apart, which is all a qualifier signature requires.
    """
    if not text:
        return None
    text = str(text)
    years = []
    for m in SPAN.finditer(text):
        start = m.group(1)
        years += [int(start), int(start[:2] + m.group(2))]
    for m in YEAR.finditer(text):
        full, short = m.group(1), m.group(2)
        if full:
            years.append(int(full))
        elif short:
            years.append(2000 + int(short))
    if not years:
        return None
    label = str(max(years))
    # A quarter is not its parent year. Without this, "Q3 FY24" and "FY24"
    # collapse into one cell and a quarterly figure reads as contradicting the
    # annual one.
    part = re.search(r"\b(q[1-4]|h[12]|[1-9]m)\b", text, re.IGNORECASE)
    if part:
        return f"{label}{part.group(1).upper()}"
    if re.search(r"\b(nine|six|three)\s+months?\b", text, re.IGNORECASE):
        return f"{label}PARTIAL"
    return label


def normalise_key(key: str) -> str:
    """Qualifier keys are free text; 'as of', 'as_of' and 'as_of_date' are one key."""
    k = re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")
    k = re.sub(r"_(date|dt)$", "", k)
    return {"asof": "as_of", "as_on": "as_of", "period_end": "period",
            "reporting_period": "period", "fiscal_year": "period",
            "financial_year": "period", "year": "period"}.get(k, k)


def demo():
    ar = normalise(81415.38, "₹ million")
    deck = normalise(8142, "Cr")
    assert ar["currency"] == "INR" and ar["value"] == 81415.38e6, ar
    assert deck["magnitude"] == 1e7, deck
    delta = abs(ar["value"] - deck["value"])
    tol = ar["tolerance"] + deck["tolerance"]
    assert comparable(ar, deck), (ar, deck)
    assert delta < tol, f"delta {delta:,.0f} should be within tolerance {tol:,.0f}"

    # ...but a genuinely different figure must not slip through the same gate.
    other = normalise(5689, "₹ Cr")
    assert abs(ar["value"] - other["value"]) > ar["tolerance"] + other["tolerance"]

    # Plural magnitude words.
    for u_str in ("Indian Rupees in millions", "INR million", "₹ million"):
        assert normalise(4724.02, u_str)["value"] == 4724.02e6, u_str
    assert normalise(8142, "crores")["magnitude"] == 1e7

    # Dimensions that cannot be compared.
    assert not comparable(normalise(1.4, "Mn Tons"), normalise(8142, "₹ Cr"))
    assert not comparable(normalise(12.7, "%"), normalise(8142, "₹ Cr"))

    # Precision follows what was printed.
    assert precision_tolerance(8142) == 0.5
    assert precision_tolerance(8140) == 5.0          # trailing zero claims less
    assert precision_tolerance(81415.38) == 0.005

    for label, want in [("FY24", "2024"), ("FY2023-24", "2024"),
                        ("2023-24", "2024"), ("FY'23", "2023"),
                        ("year ended March 31, 2024", "2024"),
                        ("March 31, 2023", "2023"), ("FY22", "2022"),
                        ("Q3 FY24", "2024Q3"), ("Q4 FY2023-24", "2024Q4"),
                        ("nine months ended December 31, 2021", "2021PARTIAL")]:
        assert normalise_period(label) == want, (label, normalise_period(label))

    for k in ("as of", "as_of", "as_of_date", "As Of"):
        assert normalise_key(k) == "as_of", (k, normalise_key(k))
    assert normalise_key("reporting period") == "period"
    print(f"units ok - AR vs deck: delta {delta:,.0f} within tolerance {tol:,.0f}")


if __name__ == "__main__":
    demo()

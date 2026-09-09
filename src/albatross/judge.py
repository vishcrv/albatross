"""The LLM pair judge, and the gate that keeps it affordable.

Numeric facts are settled by comparators in reconcile.py - deterministic,
free, and auditable. The judge exists for the cases a comparator genuinely
cannot reach: two entity-valued statements about the same subject, where
deciding between "the same fact restated", "a state that later changed", and
"a real disagreement" needs language, not arithmetic.

Ungated, this corpus offers ~40,000 candidate pairs (D6). Nearly all of them
are the model being paid to say "unrelated". The gate is deterministic and
cheap: same canonical entity, different documents, and objects that are
semantically close. Pairs are then judged several at a time, because free-tier
quota is measured in requests, not tokens.
"""
from __future__ import annotations

import itertools
import json

import numpy as np

from . import llm

OBJECT_SIMILARITY = 0.60
PAIRS_PER_CALL = 8

SCHEMA = {
    "type": "object",
    "properties": {
        "judgements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pair": {"type": "integer"},
                    "relation": {
                        "type": "string",
                        "enum": ["equivalent", "state_transition",
                                 "contradiction", "unrelated"],
                    },
                    "rationale": {"type": "string"},
                },
                "required": ["pair", "relation", "rationale"],
            },
        }
    },
    "required": ["judgements"],
}

PROMPT = """\
Each numbered pair below contains two statements about the same subject, taken
from two different documents. Decide how the two relate.

- `equivalent` - the same fact, worded differently.
- `state_transition` - both were true, at different times. Something changed
  between the documents: a role ended, a title changed, a value was superseded.
  Choose this whenever the statements are reconciled by *when* each was true.
- `contradiction` - they cannot both be true of the same subject at the same
  time, and nothing in the statements explains the difference.
- `unrelated` - they are about different attributes and do not bear on each
  other at all.

Judge only from what the statements say. Do not use outside knowledge about
these organisations or people. If a difference could be explained by timing but
neither statement gives a date, prefer `unrelated` over guessing - say so in
the rationale.

`rationale` must be one sentence, and must cite the specific wording that
decided it.

PAIRS
{pairs}

Return JSON.
"""


def _gate(conn) -> list[tuple[dict, dict]]:
    """Deterministic pre-filter. Only survivors reach the model."""
    rows = conn.execute(
        "SELECT f.*, d.title FROM facts f JOIN documents d ON d.id = f.doc_id"
        " WHERE f.value IS NULL AND f.entity_id IS NOT NULL"
    ).fetchall()
    facts = [dict(r) for r in rows]
    if not facts:
        return []

    texts = sorted({f["object_text"] for f in facts})
    vecs = np.array(llm.embed_many(texts), dtype=np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    index = {t: v for t, v in zip(texts, vecs)}

    by_entity: dict[str, list[dict]] = {}
    for f in facts:
        by_entity.setdefault(f["entity_id"], []).append(f)

    out = []
    for group in by_entity.values():
        for a, b in itertools.combinations(group, 2):
            if a["doc_id"] == b["doc_id"]:
                continue                      # cross-document only
            if a["object_text"] == b["object_text"]:
                continue                      # identical text needs no model
            sim = float(index[a["object_text"]] @ index[b["object_text"]])
            if sim >= OBJECT_SIMILARITY:
                out.append((a, b))
    return out


def _render(pairs) -> str:
    lines = []
    for i, (a, b) in enumerate(pairs):
        lines.append(f"[{i}]")
        for tag, f in (("A", a), ("B", b)):
            lines.append(f"  {tag} ({f['title']}): {f['claim_text']}")
        lines.append("")
    return "\n".join(lines)


RELATION_TO_VERDICT = {
    "equivalent": ("CORROBORATED", "JUDGE:EQUIVALENT"),
    "state_transition": ("VARIANT_EXPLAINED_BY", "JUDGE:STATE_TRANSITION"),
    "contradiction": ("CONTRADICTION", "JUDGE:CONTRADICTION"),
}


def run(conn, limit: int | None = None) -> dict:
    pairs = _gate(conn)
    if limit:
        pairs = pairs[:limit]
    results = []
    for i in range(0, len(pairs), PAIRS_PER_CALL):
        batch = pairs[i:i + PAIRS_PER_CALL]
        raw = llm.complete(PROMPT.format(pairs=_render(batch)),
                           model=llm.JUDGE_MODEL, schema=SCHEMA)
        for j in json.loads(raw).get("judgements", []):
            idx = j.get("pair")
            if not isinstance(idx, int) or not 0 <= idx < len(batch):
                continue                      # a hallucinated index is dropped
            results.append((batch[idx][0], batch[idx][1], j))
    return {"gated_pairs": len(pairs), "judged": len(results),
            "results": results}


def run_and_store(conn, limit: int | None = None) -> dict:
    """Judge the gated pairs and write verdicts, with the rationale kept.

    The rationale is the point: a verdict a reader cannot check is not an
    answer. It is stored verbatim alongside the model that produced it.
    """
    from . import reconcile

    conn.executescript(reconcile.VERDICT_SCHEMA)
    # Replace this pass's own rows. Ids are stable now, so re-running would
    # otherwise leave the previous run's verdicts alongside the new ones.
    conn.execute("DELETE FROM verdicts WHERE reason_code LIKE 'JUDGE:%'")
    out = run(conn, limit=limit)
    rows, counts = [], {}
    for a, b, j in out["results"]:
        mapped = RELATION_TO_VERDICT.get(j["relation"])
        if mapped is None:                    # 'unrelated' is not a verdict
            continue
        verdict, reason = mapped
        counts[verdict] = counts.get(verdict, 0) + 1
        rows.append((
            reconcile.verdict_id(a["id"], b["id"], "judge"),
            verdict, reason, a["id"], b["id"],
            a["entity_id"], a["predicate_id"], "{}", None, None, 1,
            "low" if (a["grounding_issues"] != "[]" or
                      b["grounding_issues"] != "[]") else "high",
            f"{j['rationale']}  [{llm.JUDGE_MODEL}]",
        ))
    conn.executemany(
        "INSERT INTO verdicts (id, verdict, reason_code, fact_a, fact_b,"
        " entity_id, predicate_id, qualifier_diff, delta, tolerance,"
        " cross_document, trust, explanation) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return {"gated": out["gated_pairs"], "stored": len(rows), **counts}

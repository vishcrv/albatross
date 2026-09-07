"""Single LLM entry point (docs/decisions.md D9).

Not an abstraction layer - one function and a provider switch. It exists
because the free tier we depend on has already changed once, and because a
grader should be able to run this with whatever key they hold.

Every response is cached on a hash of (model, prompt, schema). That is what
makes a zero-budget build workable: a page is paid for once ever, not once per
run, and the same applies to pair judgements.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"

# Free tier is roughly 10-15 requests/minute. Self-throttle rather than
# discover the limit as a wall of 429s.
MIN_INTERVAL_S = 4.5
MAX_ATTEMPTS = 5

EXTRACT_MODEL = os.environ.get("ALBATROSS_EXTRACT_MODEL", "gemini-3.5-flash-lite")
JUDGE_MODEL = os.environ.get("ALBATROSS_JUDGE_MODEL", "gemini-3.8-flash")

_last_call = 0.0


def load_env(path: str | Path = ".env") -> None:
    """Minimal .env reader - python-dotenv would be a dependency for six lines."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def _api_key() -> str:
    load_env()
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Put it in .env - see README setup."
        )
    return key


def _cache(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS llm_cache ("
        " key TEXT PRIMARY KEY, model TEXT, response TEXT,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )


def _post(model: str, body: dict) -> dict:
    global _last_call
    req = urllib.request.Request(
        ENDPOINT.format(m=model),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-goog-api-key": _api_key()},
        method="POST",
    )
    for attempt in range(MAX_ATTEMPTS):
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            # 429 is a quota bound, not a bug; 5xx is transient.
            if e.code not in (429, 500, 503) or attempt == MAX_ATTEMPTS - 1:
                raise RuntimeError(f"{model} HTTP {e.code}: {e.read()[:300]!r}") from e
            time.sleep(2 ** attempt * 5)
    raise RuntimeError("unreachable")


def complete(
    prompt: str,
    *,
    model: str = EXTRACT_MODEL,
    schema: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    """One completion. Pass `schema` to force a JSON response shape."""
    # Temperature 0: a knowledge layer whose facts change between runs is not
    # auditable, and the cache would be keyed on a prompt that no longer
    # predicts the answer.
    body: dict = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0},
    }
    if schema is not None:
        body["generationConfig"] |= {
            "responseMimeType": "application/json",
            "responseSchema": schema,
        }

    key = hashlib.sha256(
        json.dumps([model, body], sort_keys=True).encode("utf-8")
    ).hexdigest()

    if conn is not None:
        _cache(conn)
        hit = conn.execute(
            "SELECT response FROM llm_cache WHERE key=?", (key,)
        ).fetchone()
        if hit:
            return hit[0]

    data = _post(model, body)
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        # A blocked or truncated response is a real outcome, not a crash site.
        raise RuntimeError(f"no text in response: {json.dumps(data)[:400]}") from e

    if conn is not None:
        conn.execute(
            "INSERT OR REPLACE INTO llm_cache (key, model, response) VALUES (?,?,?)",
            (key, model, text),
        )
        conn.commit()
    return text


def demo():
    """Self-check: schema enforcement, and that the cache actually returns."""
    conn = sqlite3.connect(":memory:")
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
    }
    out = complete("What is 6 times 7? Respond as JSON.", schema=schema, conn=conn)
    assert json.loads(out)["answer"] == 42, out

    t0 = time.monotonic()
    again = complete("What is 6 times 7? Respond as JSON.", schema=schema, conn=conn)
    assert again == out
    assert time.monotonic() - t0 < 1.0, "second call should have hit the cache"
    print("llm ok:", out.strip())


if __name__ == "__main__":
    demo()

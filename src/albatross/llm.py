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
FALLBACK_MODEL = os.environ.get("ALBATROSS_FALLBACK_MODEL", "gemini-flash-latest")

_last_call = 0.0
_cache_conn: sqlite3.Connection | None = None

# The cache lives in its OWN file, deliberately. It used to share the knowledge
# database, and rebuilding that schema threw away every paid-for response -
# which is the exact opposite of D8, where a page is paid for once ever. The
# cache is a durable asset; the knowledge layer derived from it is disposable.
CACHE_PATH = os.environ.get("ALBATROSS_CACHE", ".llm-cache.db")


def cache() -> sqlite3.Connection:
    global _cache_conn
    if _cache_conn is None:
        _cache_conn = sqlite3.connect(CACHE_PATH)
        _cache_conn.execute(
            "CREATE TABLE IF NOT EXISTS llm_cache ("
            " key TEXT PRIMARY KEY, model TEXT, response TEXT,"
            " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        _cache_conn.execute(
            "CREATE TABLE IF NOT EXISTS embed_cache ("
            " key TEXT PRIMARY KEY, model TEXT, dims INTEGER, vec TEXT)"
        )
        _cache_conn.commit()
    return _cache_conn


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

    c = cache()
    hit = c.execute("SELECT response FROM llm_cache WHERE key=?", (key,)).fetchone()
    if hit:
        return hit[0]

    try:
        data = _post(model, body)
    except RuntimeError as e:
        # A free-tier model can be capacity-unavailable for minutes at a time.
        # Falling back beats failing a whole run; the cache key records which
        # model actually answered, so results stay attributable.
        if "503" not in str(e) or model == FALLBACK_MODEL:
            raise
        model = FALLBACK_MODEL
        key = hashlib.sha256(
            json.dumps([model, body], sort_keys=True).encode("utf-8")
        ).hexdigest()
        hit = c.execute(
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

    c.execute(
        "INSERT OR REPLACE INTO llm_cache (key, model, response) VALUES (?,?,?)",
        (key, model, text),
    )
    c.commit()
    return text


EMBED_MODEL = os.environ.get("ALBATROSS_EMBED_MODEL", "gemini-embedding-001")
EMBED_DIMS = 768
EMBED_BATCH = 100
EMBED_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{m}:batchEmbedContents"
)


def embed_many(
    texts: list[str],
    *,
    model: str = EMBED_MODEL,
) -> list[list[float]]:
    """Embed texts, batched and cached. Order matches the input."""
    if not texts:
        return []
    keys = [
        hashlib.sha256(f"{model}|{EMBED_DIMS}|{t}".encode("utf-8")).hexdigest()
        for t in texts
    ]
    out: dict[str, list[float]] = {}

    c = cache()
    for chunk in range(0, len(keys), 500):
        part = keys[chunk:chunk + 500]
        rows = c.execute(
            f"SELECT key, vec FROM embed_cache WHERE key IN"
            f" ({','.join('?' * len(part))})", part
        ).fetchall()
        out.update({r[0]: json.loads(r[1]) for r in rows})

    todo = [(k, t) for k, t in zip(keys, texts) if k not in out]
    # Deduplicate: identical claim text is common across a corpus.
    todo = list({k: t for k, t in todo}.items())

    for i in range(0, len(todo), EMBED_BATCH):
        batch = todo[i:i + EMBED_BATCH]
        body = {
            "requests": [
                {
                    "model": f"models/{model}",
                    "content": {"parts": [{"text": t}]},
                    "outputDimensionality": EMBED_DIMS,
                }
                for _, t in batch
            ]
        }
        global _last_call
        req = urllib.request.Request(
            EMBED_ENDPOINT.format(m=model),
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-goog-api-key": _api_key()},
            method="POST",
        )
        for attempt in range(MAX_ATTEMPTS):
            wait = MIN_INTERVAL_S - (time.monotonic() - _last_call)
            if wait > 0:
                time.sleep(wait)
            _last_call = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 503) or attempt == MAX_ATTEMPTS - 1:
                    raise RuntimeError(
                        f"embed HTTP {e.code}: {e.read()[:300]!r}") from e
                time.sleep(2 ** attempt * 5)

        vecs = [e["values"] for e in data["embeddings"]]
        for (k, _), v in zip(batch, vecs):
            out[k] = v
        c.executemany(
                "INSERT OR REPLACE INTO embed_cache (key, model, dims, vec)"
                " VALUES (?,?,?,?)",
                [(k, model, EMBED_DIMS, json.dumps(v))
             for (k, _), v in zip(batch, vecs)],
        )
        c.commit()

    return [out[k] for k in keys]


def demo():
    """Self-check: schema enforcement, and that the cache actually returns."""
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
    }
    out = complete("What is 6 times 7? Respond as JSON.", schema=schema)
    assert json.loads(out)["answer"] == 42, out

    t0 = time.monotonic()
    again = complete("What is 6 times 7? Respond as JSON.", schema=schema)
    assert again == out
    assert time.monotonic() - t0 < 1.0, "second call should have hit the cache"
    print("llm ok:", out.strip())


if __name__ == "__main__":
    demo()

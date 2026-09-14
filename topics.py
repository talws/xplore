"""
topics.py — group the collection into topics with an LLM.

Two passes, the same shape Mneme uses for its facets:

  1. Propose a taxonomy from a stratified sample. Asking per-tweet "what is
     this about?" produces thousands of near-synonyms and no navigable
     structure, so the vocabulary is fixed once, up front.
  2. Assign every tweet to one of those topics, in batches.

The machine's answer goes in `topics.topic`. Anything the owner edits goes in
`topics.topic_user`, and that always wins -- a re-run can never overwrite a
hand-made decision.

Resumable: assignment skips tweets already carrying a topic, so it can be
interrupted and restarted, and a later sync only classifies what arrived since.

    python topics.py --propose            # pass 1, writes the taxonomy
    python topics.py --assign --limit 500 # pass 2, a trial
    python topics.py --assign             # pass 2, everything outstanding
    python topics.py --show               # what we have
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"
KEY = os.environ.get("DEEPSEEK_API_KEY", "")

SAMPLE_SIZE = 300          # tweets shown to the taxonomy pass
BATCH = 40                 # tweets per assignment call
MAX_CHARS = 260            # per tweet sent to the model


def _call(messages: list[dict], temperature: float = 0.2,
          max_tokens: int = 3000, retries: int = 4) -> str:
    """One chat completion, with backoff. Never raises on transient failure."""
    body = json.dumps({
        "model": MODEL, "messages": messages,
        "temperature": temperature, "max_tokens": max_tokens,
    }).encode()
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(
            API_URL, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {KEY}"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.load(r)
                return d["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            last = f"http{e.code}"
            # 429 and 5xx are worth waiting out; a 400 never fixes itself.
            if e.code not in (429, 500, 502, 503, 504):
                return ""
        except Exception as e:
            last = type(e).__name__
        time.sleep(2 ** attempt * 2)
    print(f"    [!] giving up after {retries} attempts ({last})", file=sys.stderr)
    return ""


def _json_block(text: str):
    """Pull JSON out of a reply that may be wrapped in prose or fences."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        i, j = text.find(opener), text.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                continue
    return None


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def get_taxonomy(db) -> list[str]:
    row = db.execute("SELECT value FROM meta WHERE key='taxonomy'").fetchone()
    return json.loads(row["value"]) if row else []


# ── pass 1 ────────────────────────────────────────────────────────────────────

def propose(db, size: int = SAMPLE_SIZE) -> list[str]:
    """Derive the topic vocabulary from a sample of the actual collection.

    Sampled across the whole date range rather than the most recent N: this
    library spans 2012 to now, and the recent end alone would miss whole
    subjects the owner cared about years ago.
    """
    rows = db.execute("""
        SELECT text FROM tweets
        WHERE deleted=0 AND text != '' AND length(text) > 60
        ORDER BY (rowid * 2654435761) % 1000000 LIMIT ?
    """, (size,)).fetchall()
    sample = "\n---\n".join(r["text"][:MAX_CHARS].replace("\n", " ") for r in rows)

    reply = _call([
        {"role": "system", "content":
         "You build compact taxonomies for personal archives. Reply with JSON only."},
        {"role": "user", "content":
         "Below is a random sample of tweets someone bookmarked over many years.\n"
         "Propose 20-28 topics that would organise the WHOLE collection well.\n\n"
         "Rules:\n"
         "- Each topic: 1-3 words, specific enough to be useful, broad enough to "
         "hold hundreds of items.\n"
         "- Cover the actual subject matter you see. Do not invent topics that are "
         "not represented.\n"
         "- No overlapping topics, no catch-all like 'Other' or 'Miscellaneous'.\n"
         "- Reply with a JSON array of strings and nothing else.\n\n"
         f"SAMPLE:\n{sample}"}
    ], temperature=0.3, max_tokens=800)

    topics = _json_block(reply)
    if not isinstance(topics, list) or not topics:
        print("taxonomy proposal failed; raw reply follows:\n", reply[:600])
        return []
    topics = [str(t).strip() for t in topics if str(t).strip()][:30]
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('taxonomy',?)",
               (json.dumps(topics, ensure_ascii=False),))
    db.commit()
    return topics


# ── pass 2 ────────────────────────────────────────────────────────────────────

def assign(db, limit: int = 0, batch: int = BATCH) -> dict:
    topics = get_taxonomy(db)
    if not topics:
        print("no taxonomy yet — run with --propose first")
        return {}

    rows = db.execute("""
        SELECT t.id, t.author_handle, t.text FROM tweets t
        LEFT JOIN topics tp ON tp.tweet_id = t.id
        WHERE t.deleted=0 AND t.text != '' AND tp.topic IS NULL
        ORDER BY t.created_at DESC
    """).fetchall()
    if limit:
        rows = rows[:limit]
    if not rows:
        print("everything already classified")
        return {}

    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(topics))
    total, done, failed = len(rows), 0, 0
    started = time.time()
    print(f"classifying {total} tweets in batches of {batch}")

    for i in range(0, total, batch):
        chunk = rows[i:i + batch]
        listing = "\n".join(
            f'{n}| @{r["author_handle"]}: {(r["text"] or "")[:MAX_CHARS]}'.replace("\n", " ")
            for n, r in enumerate(chunk))

        reply = _call([
            {"role": "system", "content":
             "You classify text into a fixed taxonomy. Reply with JSON only."},
            {"role": "user", "content":
             f"TOPICS (choose by number):\n{numbered}\n\n"
             f"Assign each tweet below to exactly one topic number.\n"
             f"Reply with a JSON object mapping the tweet's leading number to the "
             f'topic number, e.g. {{"0": 3, "1": 12}}. Every tweet must appear.\n\n'
             f"TWEETS:\n{listing}"}
        ], max_tokens=1200)

        mapping = _json_block(reply)
        if not isinstance(mapping, dict):
            failed += len(chunk)
            continue

        writes = []
        for n, r in enumerate(chunk):
            v = mapping.get(str(n), mapping.get(n))
            try:
                ti = int(v)
            except (TypeError, ValueError):
                continue
            if 0 <= ti < len(topics):
                writes.append((r["id"], topics[ti]))

        db.executemany("""
            INSERT INTO topics(tweet_id, topic, assigned_at)
            VALUES (?,?,datetime('now'))
            ON CONFLICT(tweet_id) DO UPDATE SET
                topic=excluded.topic, assigned_at=excluded.assigned_at
        """, writes)
        db.commit()
        done += len(writes)

        rate = (i + len(chunk)) / max(time.time() - started, 1)
        eta = (total - i - len(chunk)) / max(rate, 0.01)
        print(f"  {i+len(chunk)}/{total}  assigned={done} failed={failed}  "
              f"eta {eta/60:.1f} min", flush=True)

    return {"assigned": done, "failed": failed}


def show(db):
    tax = get_taxonomy(db)
    print(f"taxonomy: {len(tax)} topics\n")
    rows = db.execute("""
        SELECT COALESCE(topic_user, topic) AS t, COUNT(*) c
        FROM topics GROUP BY 1 ORDER BY c DESC
    """).fetchall()
    for r in rows:
        print(f"  {r['c']:>6}  {r['t']}")
    un = db.execute("""
        SELECT COUNT(*) FROM tweets t LEFT JOIN topics tp ON tp.tweet_id=t.id
        WHERE t.deleted=0 AND tp.topic IS NULL
    """).fetchone()[0]
    print(f"\n  {un:>6}  (unclassified)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="xbookmarks.db")
    ap.add_argument("--propose", action="store_true")
    ap.add_argument("--assign", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=BATCH)
    args = ap.parse_args()

    if not KEY and (args.propose or args.assign):
        print("DEEPSEEK_API_KEY is not set in this shell")
        sys.exit(1)

    db = connect(args.db)
    if args.propose:
        for t in propose(db):
            print("  -", t)
    if args.assign:
        print(assign(db, args.limit, args.batch))
    if args.show or not (args.propose or args.assign):
        show(db)
    db.close()


if __name__ == "__main__":
    main()

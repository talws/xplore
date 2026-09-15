"""
ai_related.py — C2: the LLM level of Related, on top of C1.

C1 (related.py) still finds the candidates, from signals that exist in the
store, and those signals stay visible as evidence. The model adds two things:

  expand   the seed as search terms: its keywords, the people, organisations
           and places in it, and the same names in the other language
           (Greek <-> English). Greek comes back WITH accents, because the
           index does not fold them; this closes the gap found in C1. The
           terms go back into C1 as extra words, which finds paraphrases and
           cross-language matches that plain word overlap misses.
  rerank   each candidate judged against the seed: relevance 0-3 and one
           specific line saying why. Relevance orders the drawer; 0 means
           "only shares words" and sinks.

Both are cached in the database by (seed, item, model), so reopening a drawer
costs nothing, and only candidates not judged before are sent.

Any failure (no key, budget, network, bad reply) returns the plain C1 result
with the reason in `ai.error`: AI is an improvement, never a dependency.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json

import providers
import related
import urls

MAX_SEED_CHARS = 1500
MAX_ITEM_CHARS = 280
RERANK_LINKS = 12
RERANK_TWEETS = 24


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _cache_get(db, kind: str, seed: str, items: list[str], model: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(items), 400):
        chunk = items[i:i + 400]
        marks = ",".join("?" * len(chunk))
        for item, value in db.execute(
                f"SELECT item, value FROM ai_cache WHERE kind = ? AND seed = ? AND model = ? "
                f"AND item IN ({marks})", [kind, seed, model, *chunk]):
            try:
                out[item] = json.loads(value)
            except ValueError:
                pass
    return out


def _cache_put(db, kind: str, seed: str, values: dict[str, dict], model: str) -> None:
    db.executemany("INSERT OR REPLACE INTO ai_cache(kind, seed, item, model, value, created_at) "
                   "VALUES (?,?,?,?,?,?)",
                   [(kind, seed, item, model, json.dumps(v, ensure_ascii=False), _now())
                    for item, v in values.items()])
    db.commit()


def seed_key(tweet: str, url: str, q: str) -> str:
    if tweet:
        return f"t:{tweet}"
    if url:
        return f"u:{urls.match_key(url)}"
    return "q:" + hashlib.sha256(" ".join(q.lower().split()).encode()).hexdigest()[:24]


def _clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


# ── expansion ────────────────────────────────────────────────────────────────

EXPAND_SYSTEM = (
    "You turn a saved social-media post, or a search, into search terms for a personal "
    "archive of X bookmarks and news articles written in English and Greek. Answer in json.")

EXPAND_USER = """Text:
\"\"\"{text}\"\"\"

Return json in exactly this shape:
{{"keywords": ["..."], "entities": ["..."], "equivalents": ["..."]}}

- keywords: up to 8 distinctive words or short phrases central to the subject. No generic words.
- entities: up to 8 people, organisations, places or events the text names or clearly refers to.
- equivalents: the same names and key terms in the other language (Greek for English, English for Greek), plus common alternative spellings. Write Greek words with their accents."""


def expand(store, key: str, text: str) -> tuple[dict, dict]:
    """(terms, meta). Terms: {keywords, entities, equivalents}."""
    cfg = providers.load()
    model = providers.model_for(cfg, cfg["provider"], "fast")
    hit = _cache_get(store.db, "expand", key, [""], model).get("")
    if hit is not None:
        return hit, {"cached": True, "cost_usd": 0.0, "model": model}
    data, meta = providers.chat_json(store.db, [
        {"role": "system", "content": EXPAND_SYSTEM},
        {"role": "user", "content": EXPAND_USER.format(text=_clip(text, MAX_SEED_CHARS))},
    ], tier="fast", max_tokens=700, purpose="related:expand")
    terms = {k: [str(x)[:60] for x in (data.get(k) or []) if str(x).strip()][:10]
             for k in ("keywords", "entities", "equivalents")}
    _cache_put(store.db, "expand", key, {"": terms}, meta["model"])
    return terms, {"cached": False, **meta}


# ── re-rank ──────────────────────────────────────────────────────────────────

RERANK_SYSTEM = (
    "You judge which items in a personal archive of X bookmarks and news articles are "
    "genuinely related to a seed item. Related means the same story, event, argument, people "
    "or subject, not merely shared words. Answer in json.")

RERANK_USER = """Seed:
\"\"\"{seed}\"\"\"

Candidates, one per line as id | source | text:
{lines}

For every candidate give:
- relevance: 3 = the same specific story or event; 2 = closely related subject; 1 = loosely related; 0 = unrelated, or only shares words.
- why: at most 15 words naming what connects it (the event, person, claim). Empty when relevance is 0.

Return json in exactly this shape, with every candidate id exactly once:
{{"items": [{{"id": "L1", "relevance": 2, "why": "..."}}]}}"""


def _item_key(kind: str, obj: dict) -> str:
    return f"u:{urls.match_key(obj['url'])}" if kind == "link" else f"t:{obj['id']}"


def _item_line(kind: str, obj: dict) -> tuple[str, str]:
    if kind == "link":
        return obj["host"], _clip(obj.get("title") or obj["url"], MAX_ITEM_CHARS)
    text = obj.get("opener") or obj.get("text") or ""
    if obj.get("quoted"):
        text += " [quoting: " + (obj["quoted"].get("text") or "") + "]"
    link_titles = [l.get("title") for l in obj.get("links") or [] if l.get("title")]
    if link_titles:
        text += " [links: " + "; ".join(link_titles[:2]) + "]"
    return "@" + (obj.get("author_handle") or "?"), _clip(text, MAX_ITEM_CHARS)


def rerank(store, key: str, seed_text: str, links: list[dict], tweets: list[dict]) -> dict:
    """Judge candidates; returns meta. Writes `ai` = {relevance, why} onto items."""
    cfg = providers.load()
    model = providers.model_for(cfg, cfg["provider"], "fast")
    pool = [("link", l) for l in links[:RERANK_LINKS]] + [("tweet", t) for t in tweets[:RERANK_TWEETS]]
    keys = [_item_key(k, o) for k, o in pool]
    cached = _cache_get(store.db, "rerank", key, keys, model)
    todo = [(k, o, ik) for (k, o), ik in zip(pool, keys) if ik not in cached]
    meta = {"model": model, "cached": len(pool) - len(todo), "judged": 0, "cost_usd": 0.0}

    if todo:
        ids = {}
        lines = []
        for n, (kind, obj, ik) in enumerate(todo, 1):
            short = ("L" if kind == "link" else "T") + str(n)
            ids[short] = ik
            source, text = _item_line(kind, obj)
            lines.append(f"{short} | {source} | {text}")
        data, m = providers.chat_json(store.db, [
            {"role": "system", "content": RERANK_SYSTEM},
            {"role": "user", "content": RERANK_USER.format(seed=_clip(seed_text, MAX_SEED_CHARS),
                                                           lines="\n".join(lines))},
        ], tier="fast", max_tokens=60 + 40 * len(todo), purpose="related:rerank")
        fresh = {}
        for it in data.get("items") or []:
            ik = ids.get(str(it.get("id", "")).strip())
            if not ik:
                continue
            try:
                rel = max(0, min(3, int(it.get("relevance", 0))))
            except (TypeError, ValueError):
                continue
            fresh[ik] = {"relevance": rel, "why": _clip(str(it.get("why") or ""), 140) if rel else ""}
        if fresh:
            _cache_put(store.db, "rerank", key, fresh, m["model"])
        cached.update(fresh)
        meta.update(judged=len(fresh), cost_usd=m["cost_usd"], model=m["model"],
                    input_tokens=m["input_tokens"], output_tokens=m["output_tokens"])

    for (kind, obj), ik in zip(pool, keys):
        if ik in cached:
            obj["ai"] = cached[ik]
    return meta


def _order(items: list[dict], score_of) -> list[dict]:
    """Judged items by relevance, then C1 score; unjudged after, in C1 order."""
    return sorted(items, key=lambda o: (-(o["ai"]["relevance"] if "ai" in o else -1), -score_of(o)))


# ── the whole level ──────────────────────────────────────────────────────────

def related_ai(store, tweet: str = "", url: str = "", q: str = "", **kw) -> dict | None:
    base = related.related(store, tweet=tweet, url=url, q=q, **kw)
    if base is None:
        return None
    key = seed_key(tweet, url, q)
    cfg = providers.load()
    info = {"provider": cfg["provider"], "model": providers.model_for(cfg, cfg["provider"], "fast"),
            "expanded": None, "added_terms": [], "cost_usd": 0.0, "cached": 0, "judged": 0,
            "error": None}
    result = base
    try:
        terms, m1 = expand(store, key, base["seed_text"])
        info["expanded"] = terms
        info["cost_usd"] += m1.get("cost_usd", 0.0)
        extra = [t for group in terms.values() for t in group]
        if extra:
            widened = related.related(store, tweet=tweet, url=url, q=q, extra_terms=extra, **kw)
            if widened is not None:
                result = widened
                info["added_terms"] = widened.get("added_terms", [])
        m2 = rerank(store, key, result["seed_text"], result["links"], result["tweets"])
        info["cost_usd"] += m2.get("cost_usd", 0.0)
        info["cached"], info["judged"], info["model"] = m2["cached"], m2["judged"], m2["model"]
        result["links"] = _order(result["links"], lambda o: o["score"])
        result["tweets"] = _order(result["tweets"], lambda o: o["related"]["score"])
    except providers.AIError as e:
        info["error"] = str(e)
    info["cost_usd"] = round(info["cost_usd"], 6)
    result["ai"] = info
    return result

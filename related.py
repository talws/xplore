"""
related.py — C1: bookmarks and links related to a bookmark, a page, or a text.

Level 1 of the related-items engine (HANDOFF-PHASE-2 §C): no LLM, no key. The
candidates come from signals the store already has, and every result says which
signals fired. That keeps the ranking readable, and it is exactly the evidence
an LLM re-rank (C2, ai_related.py) is given; the owner wants that level too.

Signals and weights (list approved by the owner, 14 Sep):

  link    another bookmark points at the same page (urls.match_key)     5
  quote   one quotes the other, both quote the same post, or a thread   4
  words   distinctive words in common (FTS5 bm25), in the tweet or in   up to 3
          an article it links to
  author  same author                                                   1.5
  tags    a shared hashtag or user tag                                  1.5
  topic   same topic                                                    1
  time    bookmarked within `window` days of the seed                   up to 1

Topic and time on their own would match thousands of bookmarks, so they only
bring in a candidate together; otherwise they add to a score.

"Shared words" is judged by weight, not by count. Each of the seed's words
carries its inverse document frequency, and a candidate must share at least a
quarter of the seed's total. Counting words let "may", "read" and "report"
relate an AI-privacy thread to a carbon-emissions page (measured 15 Sep).
"""

from __future__ import annotations

import datetime as dt
import math
import re
import time
import unicodedata

import urls
from store import LIST_COLS, SCOPES, host_of, link_kind

WEIGHTS = {"link": 5.0, "quote": 4.0, "words": 3.0, "author": 1.5,
           "tags": 1.5, "topic": 1.0, "time": 1.0}
SIGNALS = tuple(WEIGHTS)
FULL_SCORE = 8.0          # a score this high fills the bar
MIN_MASS = 0.25           # share of the seed's word weight a candidate must have
FULL_MASS = 0.6           # a share this high earns the whole words weight
DF_CEILING = 0.03         # words in more than 3% of tweets say nothing about a subject
SEED_TEXT_CHARS = 4000

# Function words and generic vocabulary. The document-frequency ceiling catches
# the commonest; these are the ones that slip under it in a 27,000-tweet corpus
# and were seen doing damage in testing.
STOPWORDS = set("""
able about above actually after again against all already also always and any anyone
anything anyway are aren around away back because been before being below better between
big bit both but called can cannot care come comes could did didn does doesn doing don done
down during each else end enough even ever every everyone everything far feel few find first
for from further get gets give given going gone good got great had happen happened hard has
have having help her here hers herself him himself his how however into its itself just keep
kind know last least less let like little long look looking lot made make many maybe mean
means meant might more most much must myself need needs never new next not nothing now off
often once one only other our ours out over own people per place point put rather read
real really right said same say saying says see seems seen set she should show shows since
some something still such sure take talk tell than thank thanks that the their theirs them
then there these they thing things think this those though three through thus time today too
top try trying turn two under until upon use used very via wait want was wasn watch way well
were what when where whether which while who whole whom whose why will with within without
won word words work would wrong year years yes yet you your yours amp https http www com
rt lol que los las del por para con una uno como pero más mas les des est pour dans une qui
sur pas par sont avec και του της των την τον στο στη στην στον στα στις στους από απο για
που πως ότι οτι είναι ειναι δεν μην ένα ενα μια μία αλλά αλλα αυτό αυτο αυτή αυτη αυτά
αυτα έχει εχει έχουν εχουν πιο όπως οπως όταν οταν μας σας τους τις εδώ εδω εκεί εκει ήταν
ηταν πολύ πολυ ακόμα ακομα
""".replace("ς", "σ").split())    # folded like tokens(), or "της" never matches "τησ"

_WORD = re.compile(r"[^\W_]+")
_URL = re.compile(r"https?://\S+|\b\w+\.(?:com|org|net|co|ly|gr)/\S*", re.I)
_LINKS: dict = {"stamp": None, "by_key": {}}


def _latin_base(c: str) -> str:
    if ord(c) < 128:
        return c
    d = unicodedata.normalize("NFD", c)
    return d[0] if d[0].isascii() else c


def tokens(text: str) -> list[str]:
    """Words as FTS5's unicode61 tokenizer indexes them. Lower case; accents
    removed from Latin letters only (Greek keeps its tonos); and final sigma
    folded to σ, as the index stores it ("τσίπρασ", checked 15 Sep)."""
    text = (text or "").lower().replace("ς", "σ")
    return ["".join(_latin_base(c) for c in w) for w in _WORD.findall(text)]


def pick_terms(store, text: str, strict: bool = True, max_terms: int = 14) -> dict[str, float]:
    """The seed's distinctive words, each with its inverse document frequency.

    Links are removed first: a t.co code shared by a tweet and its quote looked
    like the rarest, most distinctive word of all. A word in only one tweet is
    the seed itself; one in more than DF_CEILING of tweets is too common. A
    typed query (strict=False) keeps every word the index knows.
    """
    counts: dict[str, int] = {}
    for w in tokens(_URL.sub(" ", text or "")):
        if len(w) < 3 or w in STOPWORDS or (w.isdigit() and len(w) != 4):
            continue
        counts[w] = counts.get(w, 0) + 1
    if not counts:
        return {}
    cand = list(counts)[:250]
    df = {r[0]: r[1] for r in store._in(
        "SELECT term, doc FROM tweets_fts_vocab WHERE term IN ({})", cand)}
    n = max(1, store.db.execute("SELECT COUNT(*) FROM tweets").fetchone()[0])
    idf = lambda t: math.log(n / df[t])
    if not strict:
        return {t: idf(t) for t in cand if t in df}
    ceiling = max(50, int(n * DF_CEILING))
    ranked = sorted((df[t], -counts[t], t) for t in cand if t in df and 2 <= df[t] <= ceiling)
    return {t: idf(t) for _, _, t in ranked[:max_terms]}


def share(terms: dict[str, float], text: str) -> tuple[float, list[str]]:
    """How much of the seed's word weight `text` carries, and which words."""
    have = set(tokens(text))
    got = [t for t in terms if t in have]
    total = sum(terms.values()) or 1.0
    return sum(terms[t] for t in got) / total, got


def _link_keys(store) -> dict[str, set[str]]:
    """Page key -> tweets linking it, rebuilt only when the links table changes."""
    stamp = tuple(store.db.execute(
        "SELECT COUNT(*), MAX(rowid), MAX(resolved_at) FROM links").fetchone())
    if _LINKS["stamp"] != stamp:
        by_key: dict[str, set[str]] = {}
        for tid, u in store.db.execute(
                "SELECT tweet_id, COALESCE(resolved_url, expanded_url) FROM links"):
            if u:
                by_key.setdefault(urls.match_key(u), set()).add(tid)
        _LINKS.update(stamp=stamp, by_key=by_key)
    return _LINKS["by_key"]


def _when(iso: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def related(store, tweet: str = "", url: str = "", q: str = "", window: int = 7,
            signals: set[str] | None = None, links_limit: int = 12,
            tweets_limit: int = 30, extra_terms: list[str] | None = None) -> dict | None:
    """Related links and bookmarks for one seed: a tweet id, a page URL, or text.

    `extra_terms` are more words to look for, from C2's query expansion."""
    t0 = time.time()
    db = store.db
    on = set(SIGNALS) if signals is None else set(signals) & set(SIGNALS)
    seed_row = None
    seed_ids: set[str] = set()
    seed_keys: set[str] = set()

    if tweet:
        row = db.execute("""
            SELECT t.id, t.text, t.quoted_id, t.quoted_text, t.author_handle,
                   COALESCE(t.bookmarked_at, t.first_seen) AS added_at,
                   COALESCE(tp.topic_user, tp.topic) AS topic
              FROM tweets t LEFT JOIN topics tp ON tp.tweet_id = t.id
             WHERE t.id = ? AND t.deleted = 0""", (tweet,)).fetchone()
        if not row:
            return None
        seed_row = dict(row)
        seed_ids.add(tweet)
        links = db.execute("""
            SELECT COALESCE(l.resolved_url, l.expanded_url) AS url,
                   COALESCE(NULLIF(l.title, ''), a.title) AS title
              FROM links l LEFT JOIN articles a ON a.url = COALESCE(l.resolved_url, l.expanded_url)
             WHERE l.tweet_id = ?""", (tweet,)).fetchall()
        seed_keys = {urls.match_key(r["url"]) for r in links if r["url"]}
        text = " ".join([row["text"] or "", row["quoted_text"] or ""]
                        + [r["title"] or "" for r in links])[:SEED_TEXT_CHARS]
        seed = {"type": "tweet", "id": tweet}
    elif url:
        seed_keys = {urls.match_key(url)}
        a = db.execute("SELECT title, substr(text, 1, 4000) FROM articles WHERE url = ?",
                       (url,)).fetchone()
        text = f"{a[0] or ''} {a[1] or ''}" if a else ""
        seed = {"type": "url", "url": url}
    elif q.strip():
        text = q
        seed = {"type": "q", "q": q.strip()}
    else:
        return None

    cands: dict[str, dict] = {}

    def add(tid: str, sig: str, score: float, terms=(), generates: bool = True):
        if tid in seed_ids or sig not in on:
            return
        c = cands.setdefault(tid, {"parts": {}, "terms": set(), "gen": False})
        c["parts"][sig] = max(c["parts"].get(sig, 0.0), score)
        c["terms"].update(terms)
        c["gen"] = c["gen"] or generates

    def words_score(mass: float, i: int, n: int) -> float:
        return WEIGHTS["words"] * min(1.0, mass / FULL_MASS) * (0.6 + 0.4 * (1 - i / max(1, n)))

    bm = SCOPES["bookmarks"]

    # words: tweets, then articles
    terms = pick_terms(store, text, strict=seed["type"] != "q")
    # C2 query expansion: the model's keywords, names and translations widen
    # the search. A candidate qualifies on its share of the seed's own words OR
    # of the added ones, judged separately, so added words cannot dilute the
    # seed's (a tweet sharing the seed's rare names still gets in).
    added: dict[str, float] = {}
    if extra_terms:
        # Same commonness ceiling as the seed's own words: expanding "τσιπρας"
        # also returned "ελλάδα" and "πολιτική", and with those admitted 395
        # bookmarks matched on a single common word (15 Sep).
        min_idf = math.log(1 / DF_CEILING)
        added = {t: w for t, w in pick_terms(store, " ".join(extra_terms), strict=False,
                                              max_terms=40).items()
                 if t not in terms and w >= min_idf}
    all_terms = {**terms, **added}
    # A one- or two-word query is all signal; a tweet's words need a real share.
    min_mass = MIN_MASS if seed["type"] != "q" or len(terms) > 2 else 0.0

    def weigh(candidate_text: str) -> tuple[float, list[str]]:
        mass, got = share(terms, candidate_text) if terms else (0.0, [])
        if added:
            mass2, got2 = share(added, candidate_text)
            mass, got = max(mass, mass2), got + got2
        return mass, got

    article_hits: list[tuple[str, float, list[str]]] = []
    if all_terms and "words" in on:
        match = " OR ".join(f'"{t}"' for t in all_terms)
        hits = db.execute("SELECT rowid FROM tweets_fts WHERE tweets_fts MATCH ? "
                          "ORDER BY rank LIMIT 400", (match,)).fetchall()
        info = {r["rowid"]: r for r in store._in(
            "SELECT rowid, id, text, quoted_text, sort_index FROM tweets "
            "WHERE deleted = 0 AND rowid IN ({})", [h[0] for h in hits])}
        quoted_hits: dict[str, tuple[float, list[str]]] = {}
        for i, h in enumerate(hits):
            t = info.get(h[0])
            if not t:
                continue
            mass, got = weigh(f"{t['text']} {t['quoted_text'] or ''}")
            if not got or mass < min_mass:
                continue
            s = words_score(mass, i, len(hits))
            if t["sort_index"] is not None:
                add(t["id"], "words", s, got)
            else:
                quoted_hits[t["id"]] = (s, got)
        # A hit on a quoted post counts for the bookmarks that quote it.
        for b in store._in(f"SELECT t.id, t.quoted_id FROM tweets t WHERE t.deleted = 0 "
                           f"AND {bm} AND t.quoted_id IN ({{}})", list(quoted_hits)):
            s, got = quoted_hits[b["quoted_id"]]
            add(b["id"], "words", s * 0.8, got)

        ahits = db.execute("SELECT rowid FROM articles_fts WHERE articles_fts MATCH ? "
                           "ORDER BY rank LIMIT 60", (match,)).fetchall()
        ainfo = {r["rowid"]: r for r in store._in(
            "SELECT rowid, url, title, substr(text, 1, 8000) AS text FROM articles "
            "WHERE rowid IN ({})", [h[0] for h in ahits])}
        for i, h in enumerate(ahits):
            a = ainfo.get(h[0])
            if not a or urls.match_key(a["url"]) in seed_keys:
                continue
            mass, got = weigh(f"{a['title'] or ''} {a['text'] or ''}")
            if not got or mass < min_mass:
                continue
            article_hits.append((a["url"], words_score(mass, i, len(ahits)), got))
        by_url = {u: (s, got) for u, s, got in article_hits}
        # Two indexed probes, never a join on COALESCE (see Store.find).
        for r in (store._in("SELECT tweet_id, resolved_url AS u FROM links "
                            "WHERE resolved_url IN ({})", list(by_url))
                  + store._in("SELECT tweet_id, expanded_url AS u FROM links "
                              "WHERE resolved_url IS NULL AND expanded_url IN ({})", list(by_url))):
            s, got = by_url[r["u"]]
            add(r["tweet_id"], "words", s * 0.6, got)

    # link
    if seed_keys and "link" in on:
        keys = _link_keys(store)
        for k in seed_keys:
            for tid in keys.get(k, ()):
                add(tid, "link", WEIGHTS["link"])

    if seed_row:
        added_at = seed_row["added_at"]
        # quote and thread
        if "quote" in on:
            for r in db.execute(f"""
                SELECT t.id FROM tweets t WHERE t.deleted = 0 AND {bm}
                   AND (t.quoted_id = :id OR t.id = :q OR t.quoted_id = :q)""",
                    {"id": tweet, "q": seed_row["quoted_id"]}):
                add(r[0], "quote", WEIGHTS["quote"])
            for r in db.execute("SELECT root_id FROM thread_parts WHERE id = ? "
                                "UNION SELECT id FROM thread_parts WHERE root_id = ?", (tweet, tweet)):
                add(r[0], "quote", WEIGHTS["quote"])
        # author: the nearest in time, so a prolific account cannot flood the list
        if "author" in on and seed_row["author_handle"]:
            for r in db.execute(f"""
                SELECT t.id FROM tweets t WHERE t.deleted = 0 AND {bm}
                   AND t.author_handle = ? AND t.id <> ?
                 ORDER BY abs(julianday(COALESCE(t.bookmarked_at, t.first_seen)) - julianday(?))
                 LIMIT 15""", (seed_row["author_handle"], tweet, added_at)):
                add(r[0], "author", WEIGHTS["author"])
        # tags
        if "tags" in on:
            for r in db.execute(f"""
                SELECT h2.tweet_id FROM hashtags h1
                  JOIN hashtags h2 ON h2.tag = h1.tag AND h2.tweet_id <> h1.tweet_id
                  JOIN tweets t ON t.id = h2.tweet_id AND t.deleted = 0 AND {bm}
                 WHERE h1.tweet_id = :id
                UNION
                SELECT u2.tweet_id FROM user_tags u1
                  JOIN user_tags u2 ON u2.tag = u1.tag AND u2.tweet_id <> u1.tweet_id
                 WHERE u1.tweet_id = :id
                 LIMIT 60""", {"id": tweet}):
                add(r[0], "tags", WEIGHTS["tags"])
        # topic and time together bring candidates in
        if seed_row["topic"] and {"topic", "time"} <= on:
            for r in db.execute(f"""
                SELECT t.id FROM tweets t JOIN topics tp ON tp.tweet_id = t.id
                 WHERE t.deleted = 0 AND {bm} AND COALESCE(tp.topic_user, tp.topic) = ?
                   AND abs(julianday(COALESCE(t.bookmarked_at, t.first_seen)) - julianday(?)) <= ?
                 ORDER BY abs(julianday(COALESCE(t.bookmarked_at, t.first_seen)) - julianday(?))
                 LIMIT 60""", (seed_row["topic"], added_at, window, added_at)):
                add(r[0], "topic", WEIGHTS["topic"])

    # Keep bookmarks only; add topic and time to everything that came in.
    meta = {r["id"]: r for r in store._in(f"""
        SELECT t.id, COALESCE(t.bookmarked_at, t.first_seen) AS added_at,
               COALESCE(tp.topic_user, tp.topic) AS topic
          FROM tweets t LEFT JOIN topics tp ON tp.tweet_id = t.id
         WHERE t.deleted = 0 AND {bm} AND t.id IN ({{}})""", list(cands))}
    seed_time = _when(seed_row["added_at"]) if seed_row else None
    for tid in list(cands):
        c, m = cands[tid], meta.get(tid)
        if m is None or not c["gen"]:
            del cands[tid]
            continue
        if seed_row and "topic" in on and seed_row["topic"] and m["topic"] == seed_row["topic"]:
            c["parts"]["topic"] = WEIGHTS["topic"]
        when = _when(m["added_at"])
        if seed_time and when:
            c["days"] = round(abs((when - seed_time).total_seconds()) / 86400, 1)
            if "time" in on and c["days"] <= window:
                c["parts"]["time"] = WEIGHTS["time"] * (1 - c["days"] / (window + 1))
        c["score"] = sum(c["parts"].values())

    ranked = sorted(cands.items(), key=lambda kv: (-kv[1]["score"], kv[1].get("days", 1e9)))

    # tweets
    chosen = [tid for tid, _ in ranked[:tweets_limit]]
    rows = {r["id"]: dict(r) for r in store._in(f"""
        SELECT {LIST_COLS}, COALESCE(t.bookmarked_at, t.first_seen) AS added_at,
               COALESCE(tp.topic_user, tp.topic) AS topic
          FROM tweets t LEFT JOIN topics tp ON tp.tweet_id = t.id
         WHERE t.id IN ({{}})""", chosen)}
    out_tweets = store._decorate([rows[t] for t in chosen if t in rows])
    for r in out_tweets:
        r["related"] = _explain(cands[r["id"]])

    # links: pages the related bookmarks carry, and articles that share words
    link_c: dict[str, dict] = {}

    def add_link(u: str, score: float, sigs, terms=()):
        k = urls.match_key(u) if u else None
        if not k or k in seed_keys:
            return
        c = link_c.setdefault(k, {"url": u, "score": 0.0, "signals": set(), "terms": set()})
        c["score"] = max(c["score"], score)
        c["signals"].update(sigs)
        c["terms"].update(terms)

    for u, s, got in article_hits:
        add_link(u, s, ["words"], got)
    top_ids = [tid for tid, _ in ranked[:100]]
    for r in store._in("SELECT tweet_id, COALESCE(resolved_url, expanded_url) AS u "
                       "FROM links WHERE tweet_id IN ({})", top_ids):
        c = cands[r["tweet_id"]]
        add_link(r["u"], c["score"] * 0.8, c["parts"].keys(), c["terms"])

    pool = sorted(link_c.items(), key=lambda kv: -kv[1]["score"])[:links_limit * 3]
    ulist = [c["url"] for _, c in pool]
    arts = {r["url"]: r for r in store._in(
        "SELECT url, title, status, word_count, source FROM articles WHERE url IN ({})", ulist)}
    titles = {r["u"]: r for r in store._in(
        "SELECT COALESCE(resolved_url, expanded_url) AS u, MAX(NULLIF(title, '')) AS title, "
        "MAX(article_date) AS article_date FROM links "
        "WHERE COALESCE(resolved_url, expanded_url) IN ({}) GROUP BY 1", ulist)}
    keys = _link_keys(store)
    out_links = []
    for k, c in pool:
        a, t = arts.get(c["url"]), titles.get(c["url"])
        kind = link_kind(c["url"], a["source"] if a else None, a["status"] if a else None)
        if kind not in ("article", "video"):
            continue
        out_links.append({
            # The page's stable key, as the test-set marks and eval_related.py use it.
            "key": f"u:{k}",
            "url": c["url"], "host": host_of(c["url"]), "kind": kind,
            "title": (t["title"] if t and t["title"] else None) or (a["title"] if a else None),
            "article_date": t["article_date"] if t else None,
            "archive_status": a["status"] if a else None,
            "words": a["word_count"] if a else None,
            "saved_by": len(keys.get(k, ())),
            **_explain({"score": c["score"], "parts": {s: 1 for s in c["signals"]},
                        "terms": c["terms"]}),
        })
        if len(out_links) >= links_limit:
            break
    # Archived copies first among the best matches, as agreed for the drawer.
    out_links.sort(key=lambda l: (l["archive_status"] != "ok", -l["score"]))

    return {
        "seed": seed, "seed_text": text, "terms": list(terms), "added_terms": list(added),
        "window": window,
        "signals": {s: sum(1 for c in cands.values() if s in c["parts"]) for s in SIGNALS},
        "total_tweets": len(cands), "total_links": len(link_c),
        "tweets": out_tweets, "links": out_links,
        "ms": round((time.time() - t0) * 1000),
    }


def _explain(c: dict) -> dict:
    order = sorted(c["parts"], key=lambda s: -WEIGHTS[s])
    return {"score": min(100, round(100 * c["score"] / FULL_SCORE)),
            "signals": order, "terms": sorted(c["terms"])[:6], "days": c.get("days")}

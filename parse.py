"""
parse.py — turn a raw X GraphQL `Bookmarks` response into tweet records.

Written defensively on purpose. The exact response shape has not been observed
directly: Chrome's devtools bridge never surfaced these XHRs (x.com runs a
service worker), and reading response bodies out of a logged-in session is
blocked. What *is* known is that a maintained third-party tool keys on
`url.includes('Bookmarks') && url.includes('graphql')` and survives X's
changes by parsing structurally rather than by path.

So this does not walk `data.bookmark_timeline_v2.timeline.instructions[...]`
and give up when a key moves. It recursively hunts for objects that *look like*
a tweet -- a `rest_id` next to a `legacy.full_text` -- wherever they sit. That
survives renames of every container above the tweet itself, which is where X
actually churns.

Everything here is pure: no network, no database. That makes it testable
against a saved payload the moment we have one.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Iterator

# "Wed Sep 03 17:05:10 +0000 2026" -- Twitter's long-standing created_at format.
_TW_TIME = "%a %b %d %H:%M:%S %z %Y"


def _iso(twitter_time: str | None) -> str | None:
    if not twitter_time:
        return None
    try:
        dt = datetime.datetime.strptime(twitter_time, _TW_TIME)
        return dt.astimezone(datetime.timezone.utc)\
                 .isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, TypeError):
        # Already ISO, or a format we have not seen. Keep the raw value rather
        # than dropping the only timestamp we have.
        return twitter_time


def bookmark_time(sort_index: Any) -> str | None:
    """When a bookmark was made, from its timeline entry's sortIndex.

    sortIndex is Unix milliseconds shifted left 20 bits -- not a tweet-style
    snowflake. Decoding it as one (>> 22 plus Twitter's 2010 epoch) was tried
    first and was wrong: bookmarks made on 13 September 2026 came out as
    January 2025, earlier than the tweets they bookmarked. Established on 740
    bookmarks from a live capture: the epoch is pinned between two bounds (a
    bookmark cannot predate its tweet, and cannot postdate the moment we
    captured it), and that 7-minute window contains Unix 0 and nothing else
    plausible. No bookmark predated its tweet; the tightest case, a tweet
    bookmarked the moment it was posted, fit to the second.
    """
    try:
        n = int(sort_index)
    except (TypeError, ValueError):
        return None
    ms = n >> 20
    if ms < 1_136_073_600_000:            # before 2006: not a timestamp
        return None
    dt = datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _walk(node: Any) -> Iterator[dict]:
    """Yield every dict in an arbitrarily nested JSON structure."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _unwrap(result: Any) -> dict | None:
    """Resolve a `*_results.result` node to the tweet dict itself.

    Tweets with visibility limits are wrapped one level deeper as
    `TweetWithVisibilityResults`, which is a real shape in live timelines and
    silently loses those tweets if unhandled.
    """
    if not isinstance(result, dict):
        return None
    if result.get("__typename") == "TweetWithVisibilityResults":
        inner = result.get("tweet")
        return inner if isinstance(inner, dict) else None
    return result


def _is_tweet(d: dict) -> bool:
    legacy = d.get("legacy")
    return bool(
        d.get("rest_id")
        and isinstance(legacy, dict)
        and ("full_text" in legacy or "created_at" in legacy)
    )


def _author(tweet: dict) -> tuple[str | None, str | None, str | None]:
    """(handle, display name, user id) -- tolerant of the several shapes seen."""
    user = None
    core = tweet.get("core") or {}
    for path in (("user_results", "result"), ("user_result", "result")):
        node = core
        for k in path:
            node = node.get(k) if isinstance(node, dict) else None
        if isinstance(node, dict):
            user = node
            break
    if not isinstance(user, dict):
        return None, None, None

    # Newer payloads carry `core.screen_name`; older ones use `legacy`.
    legacy = user.get("legacy") or {}
    inner_core = user.get("core") or {}
    handle = inner_core.get("screen_name") or legacy.get("screen_name")
    name = inner_core.get("name") or legacy.get("name")
    return handle, name, user.get("rest_id")


def _full_text(tweet: dict, legacy: dict) -> tuple[str, bool]:
    """Return (text, truncated).

    Long posts put the real text in `note_tweet`; `legacy.full_text` is then the
    280-char shortened form. Preferring note_tweet is what makes interception
    worth more than DOM scraping, where the remainder is simply absent.
    """
    note = tweet.get("note_tweet") or {}
    for path in (("note_tweet_results", "result", "text"),):
        node: Any = note
        for k in path:
            node = node.get(k) if isinstance(node, dict) else None
        if isinstance(node, str) and node.strip():
            return node, False

    text = legacy.get("full_text") or ""
    # X marks its own truncation; the ellipsis check catches the rest.
    truncated = bool(legacy.get("truncated")) or text.endswith("…")
    return text, truncated


def _entities(legacy: dict) -> tuple[list[dict], list[dict], list[str]]:
    """(links, media, hashtags) from a tweet's entity blocks."""
    ent = legacy.get("entities") or {}

    links = []
    for u in ent.get("urls") or []:
        expanded = u.get("expanded_url") or u.get("url")
        if not expanded:
            continue
        # Quote-tweet permalinks show up as ordinary URLs; they are not
        # outbound links and archiving them would just re-fetch x.com.
        if re.match(r"https?://(www\.)?(x|twitter)\.com/", expanded):
            continue
        links.append({
            "url": u.get("url"),
            "expanded_url": expanded,
            "display_url": u.get("display_url"),
        })

    # extended_entities carries the full media set; entities.media is a subset.
    media = []
    seen_media: set[str] = set()
    for block in (legacy.get("extended_entities") or {}, ent):
        for m in block.get("media") or []:
            url = m.get("media_url_https") or m.get("media_url")
            if not url or url in seen_media:
                continue
            seen_media.add(url)
            media.append({
                "kind": m.get("type"),
                "url": url,
                "alt": m.get("ext_alt_text") or (m.get("alt_text") or None),
            })

    hashtags = [h.get("text") for h in (ent.get("hashtags") or []) if h.get("text")]
    return links, media, hashtags


def _card_link(tweet: dict) -> dict | None:
    """The link behind a summary card, when the tweet has one.

    A card URL is often the same t.co already in entities, but not always --
    and the card is where a human-readable title lives.
    """
    card = tweet.get("card") or {}
    legacy = card.get("legacy") or card
    values = {b.get("key"): b.get("value") for b in (legacy.get("binding_values") or [])
              if isinstance(b, dict)}

    def sval(key):
        v = values.get(key)
        return v.get("string_value") if isinstance(v, dict) else None

    target = sval("card_url") or legacy.get("url")
    if not target or re.match(r"https?://(www\.)?(x|twitter)\.com/", target):
        return None
    return {"url": target, "expanded_url": target, "display_url": sval("domain"),
            "title": sval("title")}


def parse_tweet(tweet: dict) -> dict | None:
    """One tweet object -> one record for Store.upsert_tweets."""
    tweet = _unwrap(tweet) or tweet
    if not _is_tweet(tweet):
        return None
    legacy = tweet["legacy"]
    tid = str(tweet["rest_id"])

    handle, name, uid = _author(tweet)
    text, truncated = _full_text(tweet, legacy)
    links, media, hashtags = _entities(legacy)

    card = _card_link(tweet)
    if card and not any(l["expanded_url"] == card["expanded_url"] for l in links):
        links.append(card)

    quoted_id = quoted_handle = quoted_text = None
    q = _unwrap((tweet.get("quoted_status_result") or {}).get("result"))
    if q and _is_tweet(q):
        quoted_id = str(q["rest_id"])
        quoted_handle, _, _ = _author(q)
        quoted_text, _ = _full_text(q, q["legacy"])

    # A bookmark that is itself a reply, or that opens a conversation with more
    # in it, is worth flagging for the later thread pass. Cheap heuristic; the
    # pass itself confirms.
    is_thread = bool(
        legacy.get("in_reply_to_status_id_str")
        or (legacy.get("conversation_id_str")
            and str(legacy["conversation_id_str"]) != tid)
    )

    return {
        "id": tid,
        "url": f"https://x.com/{handle or 'i'}/status/{tid}",
        "author_handle": handle,
        "author_name": name,
        "author_id": uid,
        "created_at": _iso(legacy.get("created_at")),
        "text": text,
        "lang": legacy.get("lang"),
        "truncated": truncated,
        "is_thread": is_thread,
        "quoted_id": quoted_id,
        "quoted_handle": quoted_handle,
        "quoted_text": quoted_text,
        "reply_count": legacy.get("reply_count"),
        "retweet_count": legacy.get("retweet_count"),
        "like_count": legacy.get("favorite_count"),
        "source": "graphql",
        "media": media,
        "links": links,
        "hashtags": hashtags,
    }


def find_cursor(payload: Any) -> str | None:
    """The 'Bottom' cursor, i.e. where the next page starts."""
    for d in _walk(payload):
        if d.get("cursorType") == "Bottom" and isinstance(d.get("value"), str):
            return d["value"]
        # Some responses only tag the entry, not the inner content.
        if d.get("entryType") == "TimelineTimelineCursor" \
                and d.get("cursorType") == "Bottom":
            return d.get("value")
    return None


def parse_thread(payload: Any, root_id: str) -> tuple[dict | None, list[dict]]:
    """A TweetDetail response -> (the root tweet, the author's own replies).

    Only the original poster's continuation is kept. The owner's instruction was
    explicit and it is also what makes this affordable: 13,581 of these
    bookmarks have 50+ replies, so storing whole conversations would multiply
    the database for almost no gain. A self-reply chain is the thread; everyone
    else is an audience.

    Matching is by author id where available and handle otherwise, because a
    display name can change between the bookmark and the fetch.
    """
    found: dict[str, dict] = {}
    for node in _walk(payload):
        if not _is_tweet(node):
            continue
        rec = parse_tweet(node)
        if rec and (rec["id"] not in found
                    or len(rec.get("text") or "")
                    > len(found[rec["id"]].get("text") or "")):
            found[rec["id"]] = rec

    root = found.get(str(root_id))
    if not root:
        return None, []

    def same_author(r: dict) -> bool:
        if root.get("author_id") and r.get("author_id"):
            return r["author_id"] == root["author_id"]
        return bool(r.get("author_handle")) and \
            r["author_handle"] == root.get("author_handle")

    parts = [r for r in found.values()
             if r["id"] != root["id"] and same_author(r)]
    # created_at is ISO here, so lexical order is chronological.
    parts.sort(key=lambda r: (r.get("created_at") or "", r["id"]))
    return root, parts


def is_end_of_timeline(payload: Any) -> bool:
    """True when x.com says there is nothing further.

    The last page of a bookmarks walk carries only cursor entries and sets
    `stopOnEmptyResponse` on the bottom cursor. Observed at the end of the
    first full run: 26,946 tweets, then one 593-byte page shaped like this.

    That is materially different from an empty timeline caused by rate
    limiting, and the two must not be conflated -- one means stop because the
    job is done, the other means back off and try later. Before this, both
    looked like "no tweets found" and spent the failure budget.
    """
    for d in _walk(payload):
        if d.get("cursorType") == "Bottom" and d.get("stopOnEmptyResponse"):
            return True
    return False


def parse_bookmarks_response(payload: Any) -> tuple[list[dict], str | None]:
    """Raw decoded JSON -> (records, next cursor).

    Deduplicates within the page: the same tweet can appear both as a top-level
    entry and as someone else's quoted status, and only the top-level one should
    become a bookmark row -- but if we saw it *only* as a quote we still keep it,
    because it is real data we would otherwise throw away.
    """
    records: dict[str, dict] = {}
    quoted_ids: set[str] = set()

    for node in _walk(payload):
        if not _is_tweet(node):
            continue
        rec = parse_tweet(node)
        if not rec:
            continue
        q = (node.get("quoted_status_result") or {}).get("result")
        qq = _unwrap(q)
        if qq and qq.get("rest_id"):
            quoted_ids.add(str(qq["rest_id"]))
        # Prefer the richest version if the same id appears twice.
        prev = records.get(rec["id"])
        if prev is None or len(rec.get("text") or "") > len(prev.get("text") or ""):
            records[rec["id"]] = rec

    # When each bookmark was made. X never states it, but every timeline entry
    # carries a sortIndex that encodes it (see bookmark_time). Still checked
    # per bookmark rather than trusted: a value equal to the tweet's own id
    # would be posting order, not bookmark time, and a bookmark cannot predate
    # the tweet it bookmarks. sort_index is kept either way, so dates can be
    # recomputed without re-reading anything from x.com.
    for d in _walk(payload):
        eid, si = d.get("entryId"), d.get("sortIndex")
        if not (isinstance(eid, str) and eid.startswith("tweet-") and si is not None):
            continue
        rec = records.get(eid[len("tweet-"):])
        if rec is None:
            continue
        si = str(si)
        rec["sort_index"] = si
        when = bookmark_time(si)
        if when and si != rec["id"] and when >= (rec.get("created_at") or ""):
            rec["bookmarked_at"] = when

    ordered = [r for tid, r in records.items() if tid not in quoted_ids]
    ordered += [r for tid, r in records.items() if tid in quoted_ids]
    return ordered, find_cursor(payload)

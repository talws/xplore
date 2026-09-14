"""
test_parse.py — parser + store checks against a synthetic Bookmarks payload.

The real response shape has not been observed (see parse.py), so this fixture
is built from the documented/long-stable X GraphQL timeline structure. It is a
regression net, not proof: the first live capture is the real test, and
server.py quarantines anything it cannot parse so that payload can be replayed
here.

    python test_parse.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parse import parse_bookmarks_response, parse_tweet          # noqa: E402
from store import Store                                          # noqa: E402


def user(handle, name, uid):
    return {"user_results": {"result": {
        "rest_id": uid, "legacy": {"screen_name": handle, "name": name}}}}


def tweet(tid, handle, text, **kw):
    legacy = {
        "full_text": text,
        "created_at": "Wed Sep 03 17:05:10 +0000 2026",
        "lang": "en",
        "reply_count": 3, "retweet_count": 4, "favorite_count": 5,
        "conversation_id_str": kw.get("conv", tid),
        "entities": {"urls": kw.get("urls", []), "hashtags": kw.get("hashtags", [])},
    }
    if kw.get("in_reply_to"):
        legacy["in_reply_to_status_id_str"] = kw["in_reply_to"]
    if kw.get("media"):
        legacy["extended_entities"] = {"media": kw["media"]}
    t = {"rest_id": tid, "__typename": "Tweet",
         "core": user(handle, handle.title(), "u" + tid), "legacy": legacy}
    if kw.get("note"):
        t["note_tweet"] = {"note_tweet_results": {"result": {"text": kw["note"]}}}
    if kw.get("quoted"):
        t["quoted_status_result"] = {"result": kw["quoted"]}
    if kw.get("card"):
        t["card"] = {"legacy": {"binding_values": [
            {"key": "card_url", "value": {"string_value": kw["card"][0]}},
            {"key": "title", "value": {"string_value": kw["card"][1]}},
        ]}}
    return t


def snowflake(iso):
    """A sortIndex that encodes the given UTC time: Unix ms shifted 20 bits,
    plus low bits X uses to keep entries unique."""
    import datetime
    ms = int(datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    return str((ms << 20) | 12345)


def entry(tid, content, sort=None):
    e = {"entryId": f"tweet-{tid}", "content": {
        "entryType": "TimelineTimelineItem",
        "itemContent": {"itemType": "TimelineTweet", "tweet_results": {"result": content}}}}
    if sort is not None:
        e["sortIndex"] = sort
    return e


# ── fixture ───────────────────────────────────────────────────────────────────

quoted = tweet("500", "quoted_person", "The original claim being quoted.")

payload = {"data": {"bookmark_timeline_v2": {"timeline": {"instructions": [{
    "type": "TimelineAddEntries",
    "entries": [
        entry("101", tweet(
            "101", "alice", "A short bookmarked tweet with a link https://t.co/abc",
            urls=[{"url": "https://t.co/abc",
                   "expanded_url": "https://example.org/article",
                   "display_url": "example.org/article"},
                  # a quote-tweet permalink: must NOT become an outbound link
                  {"url": "https://t.co/zzz",
                   "expanded_url": "https://x.com/someone/status/999"}],
            hashtags=[{"text": "Gaza"}, {"text": "Media"}],
            media=[{"type": "photo", "media_url_https": "https://pbs.twimg.com/media/A.jpg",
                    "ext_alt_text": "A chart showing emissions falling"}]),
            sort=snowflake("2026-09-10T08:30:00Z")),

        # long-form: legacy.full_text is the truncated form, note_tweet is real
        entry("102", tweet(
            "102", "bob", "This is the shortened 280 char version…",
            note="This is the FULL untruncated long-form text that only "
                 "interception can recover, which is the whole argument for it.")),

        # visibility-wrapped: silently lost if _unwrap is missing
        # sortIndex older than the tweet itself: cannot be a bookmark time
        entry("103", {"__typename": "TweetWithVisibilityResults",
                      "tweet": tweet("103", "carol", "A limited-visibility tweet.")},
              sort=snowflake("2020-01-01T00:00:00Z")),

        # a reply -> flagged as thread; also carries a quoted tweet and a card
        entry("104", tweet("104", "dave", "Replying with commentary.",
                           in_reply_to="99", conv="99", quoted=quoted,
                           card=("https://news.example/story", "A Story Headline"))),

        {"entryId": "cursor-bottom-1", "content": {
            "entryType": "TimelineTimelineCursor",
            "cursorType": "Bottom", "value": "CURSOR_ABC"}},
    ]}]}}}}


def main():
    records, cursor = parse_bookmarks_response(payload)
    by = {r["id"]: r for r in records}

    assert cursor == "CURSOR_ABC", cursor
    # 4 bookmarks + the quoted tweet, which is kept but ordered last
    assert len(records) == 5, [r["id"] for r in records]
    assert records[-1]["id"] == "500", "quoted tweet should sort after real bookmarks"

    a = by["101"]
    assert a["author_handle"] == "alice"
    assert a["url"] == "https://x.com/alice/status/101"
    assert a["created_at"] == "2026-09-03T17:05:10Z", a["created_at"]
    assert a["like_count"] == 5 and a["reply_count"] == 3
    assert sorted(a["hashtags"]) == ["Gaza", "Media"]
    assert a["media"][0]["alt"] == "A chart showing emissions falling"
    assert a["media"][0]["kind"] == "photo"
    targets = [l["expanded_url"] for l in a["links"]]
    assert "https://example.org/article" in targets, targets
    assert not any("x.com" in t for t in targets), f"x.com link not filtered: {targets}"

    b = by["102"]
    assert b["text"].startswith("This is the FULL"), b["text"]
    assert b["truncated"] is False, "note_tweet text is complete"

    assert "103" in by, "TweetWithVisibilityResults was dropped"
    assert by["103"]["author_handle"] == "carol"

    d = by["104"]
    assert d["is_thread"] is True, "a reply should be flagged as thread"
    assert d["quoted_id"] == "500" and d["quoted_handle"] == "quoted_person"
    assert any(l.get("title") == "A Story Headline" for l in d["links"]), d["links"]

    # bookmark time from sortIndex, with both sanity checks
    assert a["bookmarked_at"] == "2026-09-10T08:30:00Z", a.get("bookmarked_at")
    assert "bookmarked_at" not in by["103"] and by["103"]["sort_index"], by["103"]
    assert "sort_index" not in b and "bookmarked_at" not in b, b

    # truncation detection on the ellipsis form
    t = parse_tweet(tweet("900", "eve", "cut off here…"))
    assert t["truncated"] is True

    # ── store round-trip ──────────────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as tmp:
        s = Store(os.path.join(tmp, "t.db"))
        # Dates arriving on a re-walk of tweets already held, which is how the
        # backfill gets them: first store without dates, then with.
        undated = [{k: v for k, v in r.items() if k not in ("sort_index", "bookmarked_at")}
                   for r in records]
        r0 = s.upsert_tweets(undated)
        assert r0["added"] == 5 and r0["dated"] == 0, r0

        r1 = s.upsert_tweets(records)
        assert r1["added"] == 0 and r1["dated"] == 1, r1
        assert r1["links"] >= 2, r1

        r2 = s.upsert_tweets(records)          # idempotent
        assert r2["added"] == 0 and r2["updated"] == 5 and r2["dated"] == 0, r2

        def bookmarked(tid):
            return s.db.execute("SELECT bookmarked_at FROM tweets WHERE id=?",
                                (tid,)).fetchone()[0]
        assert bookmarked("101") == "2026-09-10T08:30:00Z"
        s.upsert_tweets(undated)               # a capture without dates keeps them
        assert bookmarked("101") == "2026-09-10T08:30:00Z"
        assert bookmarked("103") is None

        st = s.stats()
        assert st["tweets"] == 5, st
        assert st["uniqueLinks"] >= 2 and st["linksPending"] >= 2, st

        hits = s.search("untruncated")
        assert len(hits) == 1 and hits[0]["id"] == "102", hits

        assert {h["id"] for h in s.search(author="alice")} == {"101"}

        # A later expansion pass must survive a subsequent re-scrape. The
        # recovered text has to be genuinely longer than the timeline form --
        # that length comparison *is* the guard being tested.
        recovered = ("A much longer recovered version of the tweet text, "
                     "restored by the expansion pass and comfortably longer "
                     "than the 280-character timeline form it replaces.")
        s.upsert_tweets([{"id": "101", "text": recovered, "truncated": False}])
        assert s.search(author="alice")[0]["text"] == recovered

        s.upsert_tweets(records)               # re-scrape with the short form
        got = s.search(author="alice")[0]["text"]
        assert got == recovered, f"re-scrape clobbered recovered text: {got!r}"

        assert s.known_ids(["101", "999"]) == {"101"}
        assert len(s.pending_links(10)) >= 2
        s.close()                              # Windows will not delete an open DB

    print(f"all checks passed — {len(records)} records, cursor={cursor}")


if __name__ == "__main__":
    main()

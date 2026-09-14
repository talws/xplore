"""
redate_bookmarks.py — recompute bookmarked_at from the stored sort_index.

Bookmark times are derived, not captured: parse.bookmark_time() decodes x.com's
sortIndex, which is kept verbatim in tweets.sort_index. So when the decoding
changes -- as it did once, from a wrong snowflake reading to the right one --
every date can be recomputed here without reading anything from x.com again.

Applies the same checks as the parser: a sort_index equal to the tweet id is
posting order, not bookmark time, and a bookmark cannot predate its tweet.
Rows failing either get bookmarked_at cleared.

    python redate_bookmarks.py            # dry run
    python redate_bookmarks.py --apply
"""

from __future__ import annotations

import argparse
import sqlite3

from parse import bookmark_time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="xbookmarks.db")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(args.db, timeout=30)
    db.execute("PRAGMA busy_timeout = 30000")
    rows = db.execute("SELECT id, sort_index, created_at, bookmarked_at FROM tweets "
                      "WHERE sort_index IS NOT NULL OR bookmarked_at IS NOT NULL").fetchall()

    changed = cleared = 0
    for tid, si, created, old in rows:
        when = bookmark_time(si) if si is not None else None
        if when and (si == tid or when < (created or "")):
            when = None
        if when == old:
            continue
        changed += 1
        cleared += when is None
        if args.apply:
            db.execute("UPDATE tweets SET bookmarked_at = ? WHERE id = ?", (when, tid))
            db.commit()

    dated = db.execute("SELECT COUNT(*) FROM tweets WHERE bookmarked_at IS NOT NULL").fetchone()[0]
    print(f"{'changed' if args.apply else 'would change'} {changed} rows "
          f"({cleared} cleared); {dated if args.apply else 'n/a'} tweets dated")
    db.close()


if __name__ == "__main__":
    main()

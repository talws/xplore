"""
retrim_articles.py — apply archive_links.trim_tail() to articles already stored.

extract() now cuts comment threads and page tails, but only for pages fetched
from here on. The stored text is kept, so existing rows can be trimmed without
fetching anything again.

A trim only ever lowers a word count, so it can turn "ok" into "thin" but never
the reverse; rows that were not "ok" keep their status. Transcripts are skipped:
they never went through extract().

    python retrim_articles.py              # dry run: what would change
    python retrim_articles.py --apply
"""

from __future__ import annotations

import argparse
import sqlite3

from archive_links import classify, trim_tail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="xbookmarks.db")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(args.db, timeout=30)
    db.execute("PRAGMA busy_timeout = 30000")
    rows = db.execute(
        "SELECT url, title, text, word_count, status FROM articles "
        "WHERE text IS NOT NULL AND text != '' AND COALESCE(source, '') != 'transcript'"
    ).fetchall()

    changed = removed = downgraded = 0
    for url, title, text, words, status in rows:
        trimmed = trim_tail(text)
        if trimmed == text:
            continue
        new_status, new_words = classify(title, trimmed)
        if status != "ok":
            new_status = status
        changed += 1
        removed += (words or 0) - new_words
        if new_status != status:
            downgraded += 1
            print(f"  {status} -> {new_status}  {words} -> {new_words}  {url}")
        if args.apply:
            db.execute("UPDATE articles SET text = ?, word_count = ?, status = ? WHERE url = ?",
                       (trimmed, new_words, new_status, url))
            db.commit()

    print(f"{'trimmed' if args.apply else 'would trim'} {changed} articles, "
          f"{removed:,} words of page tail, {downgraded} no longer count as articles")
    db.close()


if __name__ == "__main__":
    main()

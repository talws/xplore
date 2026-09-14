"""
urls.py — one set of rules for cleaning article addresses.

Why it matters: archive.today matches addresses exactly. Verified 2026-09-13 on
live snapshots -- the clean NYT address redirected to its snapshot, the same
address plus NYT's share parameters returned 404. On six real worklist articles
carrying junk, the stored address found 1 snapshot and the cleaned one found 4.

Stored links carry several kinds of noise:

  * Escaped ampersands. resolve_links.py read t.co's destination out of an HTML
    attribute and kept "&amp;" (and one "&#38;"), so parameter names arrived as
    "amp;utm_campaign". Only the ampersand escapes are undone here. Full HTML
    unescaping is wrong: Python's html.unescape reads "&section" as the legacy
    entity "&sect" and turns NYT's "...&section=Opinion" into "§ion=Opinion",
    in URLs that were never escaped at all.
  * Tracking parameters, global (utm_*, fbclid) or site-specific: "module",
    "section" or "r" are junk on nytimes.com or Substack but could be the
    address on another site, so those rules are scoped to their host.
  * Wrappers: Outlook SafeLinks, justice.gov's age check, and ";jsessionid="
    inside a path. Unwrapped to the address they carry.

Parameters that survive are kept byte-for-byte. The query is filtered, never
decoded and re-encoded: urlencode would rewrite %2C and "+", which is itself
enough for an exact-match lookup to miss.

  for_original()  noise removed, gift and subscriber tokens kept -- they open
                  the paywall for the owner on the publisher's site.
  for_archive()   noise and tokens removed -- snapshots are filed under the
                  plain address.
  match_key()     comparison key. Strips at least what for_archive() strips,
                  because capture.py maps a snapshot back to its bookmark by
                  comparing keys. Host case is ignored; path case is not unless
                  loose=True, since bit.ly/AbC and bit.ly/abc are different links.

Every site-specific name was read off the real links in xbookmarks.db.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, unquote_plus, urlsplit, urlunsplit

# &amp; &#38; &#038; &#x26; -- and nothing else.
_AMP = re.compile(r"&(?:amp|#0*38|#x0*26);", re.I)

TRACKING = frozenset({
    # generic analytics
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "utm_name", "utm_reader", "utm_place", "utm_userid", "utm_cid",
    "utm_social_handle_id", "utm_social_post_id",
    "fbclid", "gclid", "msclkid", "dclid", "gbraid", "wbraid", "igshid", "igsh",
    "mibextid", "_ga", "_gl", "mc_cid", "mc_eid", "cmpid", "ocid", "ito",
    "__twitter_impression", "ref_src", "ref_url", "referringsource", "reflink",
    "leadsource", "etear",
    # publisher-specific but unambiguous
    "taid",                              # AP News (TrueAnthem)
    "smid", "smtyp", "sharetype", "sref",  # NYT sharing
    "dcmp",                              # CNN
    "mod",                               # WSJ
    "itid",                              # Washington Post
    "xtor",                              # Le Monde / AT Internet
    "si", "feature",                     # YouTube and share sheets
    "srnd",                              # Haaretz
    "embedded-checkout", "showwelcomeonshare", "sessionid", "ouid", "rtpof",
})

# Junk on these sites, too generic to strip anywhere else. "name*" is a prefix.
HOST_TRACKING = {
    "nytimes.com": ("action", "module", "pgtype", "region", "context", "section"),
    "substack.com": ("r", "triedredirect"),
    "cnn.com": ("date", "profile"),
    "docs.google.com": ("usp", "sd"),
    "facebook.com": ("fs", "s", "ref"),
    "springer.com": ("error", "code"),       # cookie-redirect leftovers
    "academic.oup.com": ("login",),
    "ft.com": ("syn-*",),
}

ACCESS = frozenset({
    "unlocked_article_code",                 # NYT gift link
    "pwapi_token",                           # Washington Post gift link
    "gaa_at", "gaa_n", "gaa_ts", "gaa_sig",  # Google "Subscribe with Google"
})
HOST_ACCESS = {
    "wsj.com": ("st",),                      # WSJ gift link
}

# Too ambiguous to strip from a link someone will open, but safe to ignore when
# deciding whether two addresses are the same page.
_MATCH_ONLY = frozenset({"ref", "s", "t", "share", "sh", "cbrd", "ucbcb", "amp"})

_JSESSION = re.compile(r";jsessionid=[^/?#]*", re.I)


def unescape_amp(raw: str) -> str:
    """&amp; -> &, repeatedly (for &amp;amp;). Leaves every other entity alone."""
    s = (raw or "").strip()
    prev = None
    while prev != s:
        prev, s = s, _AMP.sub("&", s)
    return s


def _host_of(p) -> str:
    return (p.hostname or "").lower().removeprefix("www.")


def _on(host: str, table: dict) -> tuple:
    out = ()
    for suffix, names in table.items():
        if host == suffix or host.endswith("." + suffix):
            out += names
    return out


def _unwrap(s: str) -> str:
    """The address inside a wrapper, or s unchanged."""
    try:
        p = urlsplit(s)
    except ValueError:
        return s
    host = _host_of(p)
    q = {k.lower(): v for k, v in parse_qsl(p.query, keep_blank_values=True)}
    if host.endswith("safelinks.protection.outlook.com") and q.get("url", "").startswith("http"):
        return q["url"]
    if host.endswith("justice.gov") and p.path.rstrip("/").endswith("/age-verify") \
            and q.get("destination", "").startswith("/"):
        return f"{p.scheme}://{p.netloc}{quote(q['destination'], safe='/%:@!$&()*+,;=-._~')}"
    return s


def _is_noise(key: str, host: str, drop_access: bool, extra: frozenset) -> bool:
    k = unquote_plus(key).lower()
    k = k[4:] if k.startswith("amp;") else k      # partly escaped leftovers
    k = k.rstrip("=")                             # "cmpid=" as stored
    if k.startswith("utm_") or k in TRACKING or k in extra:
        return True
    names = _on(host, HOST_TRACKING) + (_on(host, HOST_ACCESS) if drop_access else ())
    if drop_access and k in ACCESS:
        return True
    return any(k.startswith(n[:-1]) if n.endswith("*") else k == n for n in names)


def _rebuild(raw: str, drop_access: bool, extra: frozenset = frozenset()) -> str:
    s = unescape_amp(raw)
    for _ in range(2):                 # a wrapper inside a wrapper, at most
        inner = _unwrap(s)
        if inner == s:
            break
        s = unescape_amp(inner)
    try:
        p = urlsplit(s)
    except ValueError:
        return s
    if not p.scheme or not p.netloc:
        return s
    host = _host_of(p)
    path = _JSESSION.sub("", p.path)
    # Filter the raw pieces; never decode and re-encode what is kept.
    kept = [piece for piece in p.query.split("&")
            if piece and not _is_noise(piece.split("=", 1)[0], host, drop_access, extra)]
    # The fragment goes: it never selects a different document, and a "#" would
    # end archive.today's /newest/ address early.
    return urlunsplit((p.scheme, p.netloc, path, "&".join(kept), ""))


def for_original(raw: str) -> str:
    """For opening the publisher's page: noise removed, gift tokens kept."""
    return _rebuild(raw, drop_access=False)


def for_archive(raw: str) -> str:
    """For archive lookups: noise and access tokens removed."""
    return _rebuild(raw, drop_access=True)


def match_key(raw: str, loose: bool = False) -> str:
    """Comparison key: scheme-less, www-less, host lower-cased, no trailing slash.

    loose=True also lower-cases path and query. capture.py only uses it as a
    fallback, and only when it points at exactly one article.
    """
    s = _rebuild(raw, drop_access=True, extra=_MATCH_ONLY)
    try:
        p = urlsplit(s)
    except ValueError:
        return s.lower()
    key = _host_of(p) + (p.path.rstrip("/") or "/") + ("?" + p.query if p.query else "")
    return key.lower() if loose else key


def archive_today(raw: str) -> str:
    return "https://archive.ph/newest/" + for_archive(raw)


def wayback(raw: str) -> str:
    return "https://web.archive.org/web/2*/" + for_archive(raw)


if __name__ == "__main__":
    NYT = "https://www.nytimes.com/2022/05/04/opinion/is-more-war.html"
    cases = [
        # (input, for_original, for_archive)
        ("https://apnews.com/article/x-123?taid=674ef64c&amp;utm_campaign=TrueAnthem&amp;utm_medium=AP",
         "https://apnews.com/article/x-123", "https://apnews.com/article/x-123"),
        ("https://www.haaretz.com/a/b?utm_source=mailchimp&amp;utm_medium=Content&amp;utm_campaign=haaretz-today&amp;utm_content=00a78f143",
         "https://www.haaretz.com/a/b", "https://www.haaretz.com/a/b"),
        (NYT + "?unlocked_article_code=1.abc&smid=url-share",
         NYT + "?unlocked_article_code=1.abc", NYT),
        # the &section -> § corruption, escaped and unescaped
        (NYT + "?action=click&amp;module=Well&amp;pgtype=Homepage&amp;section=Opinion", NYT, NYT),
        (NYT + "?action=click&module=Well&pgtype=Homepage&section=Opinion", NYT, NYT),
        ("https://example.org/p?a=1&section=world", "https://example.org/p?a=1&section=world",
         "https://example.org/p?a=1&section=world"),
        ("https://adamtooze.substack.com/p/top-links?r=4bdb4i&#38;utm_campaign=post&#38;utm_medium=web",
         "https://adamtooze.substack.com/p/top-links", "https://adamtooze.substack.com/p/top-links"),
        ("https://www.wsj.com/opinion/x-0c430244?st=EwYmBn&mod=share",
         "https://www.wsj.com/opinion/x-0c430244?st=EwYmBn", "https://www.wsj.com/opinion/x-0c430244"),
        ("https://www.cnn.com/2024/09/14/americas/x?Date=20240914&Profile=cnnbrk",
         "https://www.cnn.com/2024/09/14/americas/x", "https://www.cnn.com/2024/09/14/americas/x"),
        ("https://docs.google.com/document/d/12Qg/edit?usp=sharing&ouid=1071&rtpof=true&sd=true",
         "https://docs.google.com/document/d/12Qg/edit", "https://docs.google.com/document/d/12Qg/edit"),
        ("https://oxfam.example/bitstream/handle/1/2/bp.pdf;jsessionid=AB12CD?sequence=4",
         "https://oxfam.example/bitstream/handle/1/2/bp.pdf?sequence=4",
         "https://oxfam.example/bitstream/handle/1/2/bp.pdf?sequence=4"),
        ("https://nam04.safelinks.protection.outlook.com/?url=https%3A%2F%2Fwww.science.org%2Fcontent%2Farticle%2Frich-countries&data=05%7C02",
         "https://www.science.org/content/article/rich-countries", "https://www.science.org/content/article/rich-countries"),
        ("https://www.justice.gov/age-verify?destination=/epstein/files/DataSet%209/EFTA01010128.pdf",
         "https://www.justice.gov/epstein/files/DataSet%209/EFTA01010128.pdf",
         "https://www.justice.gov/epstein/files/DataSet%209/EFTA01010128.pdf"),
        ("https://www.dropbox.com/scl/fi/bf4m/Letter.pdf?rlkey=z3ek&amp;e=1&amp;dl=0",
         "https://www.dropbox.com/scl/fi/bf4m/Letter.pdf?rlkey=z3ek&e=1&dl=0",
         "https://www.dropbox.com/scl/fi/bf4m/Letter.pdf?rlkey=z3ek&e=1&dl=0"),
        ("https://www.google.com/maps/d/viewer?mid=1V8N&ll=48.39%2C31.11&z=6",
         "https://www.google.com/maps/d/viewer?mid=1V8N&ll=48.39%2C31.11&z=6",
         "https://www.google.com/maps/d/viewer?mid=1V8N&ll=48.39%2C31.11&z=6"),
        ("https://example.org/s?q=a+b&utm_source=x", "https://example.org/s?q=a+b", "https://example.org/s?q=a+b"),
        ("https://www.ft.com/content/3738ba48?syn-25a6b1a6=1", "https://www.ft.com/content/3738ba48",
         "https://www.ft.com/content/3738ba48"),
        ("https://bucket.s3.amazonaws.com/f.pdf?X-Amz-Algorithm=AWS4&X-Amz-Signature=zz&utm_source=x",
         "https://bucket.s3.amazonaws.com/f.pdf?X-Amz-Algorithm=AWS4&X-Amz-Signature=zz",
         "https://bucket.s3.amazonaws.com/f.pdf?X-Amz-Algorithm=AWS4&X-Amz-Signature=zz"),
        ("https://example.org/article.php?id=42&utm_medium=social#comments",
         "https://example.org/article.php?id=42", "https://example.org/article.php?id=42"),
    ]
    for raw, orig, arch in cases:
        assert for_original(raw) == orig, ("original", raw, for_original(raw))
        assert for_archive(raw) == arch, ("archive", raw, for_archive(raw))
        assert "§" not in for_archive(raw) and "§" not in match_key(for_archive(raw)), raw
        assert match_key(for_archive(raw)) == match_key(raw), ("invariant", raw)
        assert match_key(for_archive(raw), True) == match_key(raw, True), ("invariant loose", raw)
        assert for_archive(for_archive(raw)) == for_archive(raw), ("idempotent", raw)
        assert for_original(for_original(raw)) == for_original(raw), ("idempotent", raw)
    assert match_key("https://bit.ly/AbC") != match_key("https://bit.ly/abc")
    assert match_key("https://bit.ly/AbC", True) == match_key("https://bit.ly/abc", True)
    assert match_key("http://Aljazeera.com") == match_key("https://www.aljazeera.com/")
    print(f"urls.py: {len(cases)} cases pass")

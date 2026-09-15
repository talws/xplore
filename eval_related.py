"""
eval_related.py — does AI make Related better? Measured on the owner's judgements.

The C2 gate (HANDOFF-PHASE-2 §C): "the gold set shows embeddings or re-rank
beating C1". "Related" cannot be judged by code, so the owner marks items in
the Related drawer as relevant or not; the marks are kept in a gold file
outside the repo, because bookmark ids are personal data:

    %USERPROFILE%\\.xplore\\gold.json
    {"seeds": {"t:<tweet id>": {"seed": {"tweet": "<id>"},
                               "relevant": ["t:<id>", "u:<page key>", ...],
                               "not": [...], "updated": "..."}}}

For every seed with judgements this runs three variants and scores the top 10
bookmarks and the top 10 links of each:

    c1        related.related                        signals only, no AI
    expand    C1 with the model's extra search terms   (no re-rank)
    ai        ai_related.related_ai                  expansion + re-rank

Two precision figures, because unjudged items are unknown rather than wrong:

    judged precision   relevant / judged, among the top 10
    strict precision   relevant / 10, unjudged counted as not relevant

The AI variants use the configured provider and are cached, so a second run
costs nothing unless new candidates appear.

    python eval_related.py                    # all three variants
    python eval_related.py --no-ai            # C1 only, no API calls
    python eval_related.py --db D:/copy.db --gold D:/gold.json --out report.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import ai_related
import providers
import related
import urls
from store import Store

GOLD_PATH = os.path.join(os.path.dirname(providers.CONFIG_PATH), "gold.json")
TOP = 10


def load_gold(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"seeds": {}}


_gold_lock = __import__("threading").Lock()


def item_key(item: dict) -> str:
    """t:<tweet id> for a bookmark, u:<page key> for a link: the keys the
    report scores and the Related drawer marks."""
    if item.get("tweet"):
        return f"t:{item['tweet']}"
    if item.get("url"):
        return f"u:{urls.match_key(item['url'])}"
    raise ValueError("an item is a tweet id or a url")


def judgements(seed: dict, path: str = GOLD_PATH) -> dict:
    key = ai_related.seed_key(seed.get("tweet", ""), seed.get("url", ""), seed.get("q", ""))
    entry = load_gold(path).get("seeds", {}).get(key) or {}
    return {"seedKey": key, "relevant": entry.get("relevant") or [], "not": entry.get("not") or []}


def mark(seed: dict, item: dict, verdict: str, path: str = GOLD_PATH) -> dict:
    """Record the owner's verdict on one item for one seed: relevant, not, or
    clear. Written atomically; the file holds personal data, so it lives beside
    the settings, never in the repo."""
    if verdict not in ("relevant", "not", "clear"):
        raise ValueError("verdict is relevant, not or clear")
    seed = {k: str(v) for k, v in seed.items() if k in ("tweet", "url", "q") and v}
    if not seed:
        raise ValueError("a seed is a tweet id, a url or a query")
    key = ai_related.seed_key(seed.get("tweet", ""), seed.get("url", ""), seed.get("q", ""))
    ik = item_key(item)
    with _gold_lock:
        gold = load_gold(path)
        entry = gold.setdefault("seeds", {}).setdefault(key, {"seed": seed, "relevant": [], "not": []})
        for side in ("relevant", "not"):
            entry[side] = [k for k in entry.get(side, []) if k != ik]
        if verdict != "clear":
            entry[verdict].append(ik)
        entry["updated"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(gold, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    return judgements(seed, path)


def item_keys(result: dict) -> tuple[list[str], list[str]]:
    tweets = [f"t:{t['id']}" for t in result["tweets"]]
    links = [f"u:{urls.match_key(l['url'])}" for l in result["links"]]
    return tweets, links


def score(keys: list[str], relevant: set[str], judged_not: set[str]) -> dict:
    top = keys[:TOP]
    rel = sum(1 for k in top if k in relevant)
    judged = sum(1 for k in top if k in relevant or k in judged_not)
    return {"relevant": rel, "judged": judged, "shown": len(top),
            "judged_precision": round(rel / judged, 3) if judged else None,
            "strict_precision": round(rel / TOP, 3)}


def run(store: Store, gold: dict, use_ai: bool) -> dict:
    variants = ["c1"] + (["expand", "ai"] if use_ai else [])
    per_seed = []
    for key, entry in gold.get("seeds", {}).items():
        relevant = set(entry.get("relevant") or [])
        judged_not = set(entry.get("not") or [])
        if not relevant and not judged_not:
            continue
        seed = entry.get("seed") or {}
        row = {"seed": key, "variants": {}}
        base = related.related(store, **seed)
        if base is None:
            row["error"] = "seed no longer exists"
            per_seed.append(row)
            continue
        results = {"c1": base}
        if use_ai:
            try:
                terms, _ = ai_related.expand(store, key, base["seed_text"])
                extra = [t for group in terms.values() for t in group]
                results["expand"] = related.related(store, extra_terms=extra, **seed) or base
                results["ai"] = ai_related.related_ai(store, **seed)
                if results["ai"]["ai"]["error"]:
                    row["ai_error"] = results["ai"]["ai"]["error"]
            except providers.AIError as e:
                row["ai_error"] = str(e)
                results = {"c1": base}
        for v, res in results.items():
            tweets, links = item_keys(res)
            row["variants"][v] = {"tweets": score(tweets, relevant, judged_not),
                                  "links": score(links, relevant, judged_not)}
        per_seed.append(row)

    summary = {}
    for v in variants:
        for kind in ("tweets", "links"):
            rows = [r["variants"][v][kind] for r in per_seed if v in r.get("variants", {})]
            jp = [r["judged_precision"] for r in rows if r["judged_precision"] is not None]
            summary[f"{v}.{kind}"] = {
                "seeds": len(rows),
                "judged_precision": round(sum(jp) / len(jp), 3) if jp else None,
                "strict_precision": round(sum(r["strict_precision"] for r in rows) / len(rows), 3) if rows else None,
            }
    return {"top": TOP, "variants": variants, "summary": summary, "seeds": per_seed}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default="xbookmarks.db")
    ap.add_argument("--gold", default=GOLD_PATH)
    ap.add_argument("--no-ai", action="store_true", help="C1 only; no API calls")
    ap.add_argument("--out", help="also write the full report as JSON here")
    args = ap.parse_args()

    gold = load_gold(args.gold)
    n = sum(1 for e in gold.get("seeds", {}).values() if e.get("relevant") or e.get("not"))
    if not n:
        print(f"No judgements in {args.gold} yet. Mark items as relevant or not in the Related drawer first.")
        return
    store = Store(args.db)
    report = run(store, gold, use_ai=not args.no_ai)

    print(f"\n{n} judged seeds · precision in the top {TOP}\n")
    print(f"  {'variant':18s} {'judged precision':>17s} {'strict precision':>17s}")
    for name, s in report["summary"].items():
        jp = "-" if s["judged_precision"] is None else f"{s['judged_precision']:.2f}"
        sp = "-" if s["strict_precision"] is None else f"{s['strict_precision']:.2f}"
        print(f"  {name:18s} {jp:>17s} {sp:>17s}")
    errors = [r for r in report["seeds"] if r.get("ai_error") or r.get("error")]
    if errors:
        print(f"\n  {len(errors)} seed(s) had problems, e.g. {errors[0].get('ai_error') or errors[0].get('error')}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
        print(f"\n  full report: {args.out}")


if __name__ == "__main__":
    main()

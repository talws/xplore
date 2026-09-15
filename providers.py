"""
providers.py — the LLM layer: which provider, which key, the call, its cost.

Owner decision (14 Sep): a paid API with the owner's key, DeepSeek for now, and
the provider must be changeable. The list follows Compendium II's
core/ai_client.py, so both apps offer the same choices with a fast and a quality
model each.

Every callable provider here speaks the OpenAI-compatible chat-completions
protocol, so plain HTTP is enough and no SDK is installed. Claude (Anthropic)
is listed but not callable yet: its adapter should use the official `anthropic`
SDK, which is not installed. Embeddings have a slot in the design (C2 plan) but
no provider: DeepSeek offers none.

Keys are stored outside the repo and the database, in
%USERPROFILE%\\.xplore\\config.json, or come from an environment variable such
as DEEPSEEK_API_KEY. They are never logged, never returned by the API, never
written into the page: the settings only say whether a key is set, where from,
and its last four characters.

Every call records its tokens and cost in the `ai_usage` table. Two hard caps,
both editable: per request (checked against an upper-bound estimate before the
call) and per day (UTC). Prices are the provider's PEAK rates, so the recorded
cost is an upper bound too.

Reasoning is switched OFF by default. Xplore's jobs are short and structured
(list search terms, score candidates), and a reasoning model spends its output
allowance thinking first: deepseek-flash reasons by default, and on 15 Sep
271 of 319 output tokens were reasoning, so replies hit max_tokens before the
JSON ("finish: length"). Pass reasoning=True where it is worth paying for.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

CONFIG_PATH = os.environ.get("XPLORE_CONFIG") or os.path.join(
    os.path.expanduser("~"), ".xplore", "config.json")

# prices: $ per 1M tokens (input, cached input, output), at peak rates.
# quiet: request fields that switch reasoning off, where the provider has them.
PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek", "protocol": "openai", "env": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com",
        "fast": "deepseek-flash", "quality": "deepseek-v4-pro",
        # Checked 15 Sep 2026 at api-docs.deepseek.com (pricing, thinking mode)
        # and GET /models. Peak is 01-04 and 06-10 UTC on weekdays; off-peak is half.
        "prices": {"deepseek-flash": (0.30, 0.006, 1.20),
                   "deepseek-v4-pro": (1.32, 0.044, 3.96)},
        "quiet": {"thinking": {"type": "disabled"}},
        "json_mode": True,
    },
    "anthropic": {
        "label": "Claude (Anthropic)", "protocol": "anthropic", "env": "ANTHROPIC_API_KEY",
        "fast": "claude-haiku-4-5", "quality": "claude-opus-5",
        "available": False, "note": "needs the anthropic SDK, which is not installed yet",
    },
    "openai": {
        "label": "GPT (OpenAI)", "protocol": "openai", "env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "fast": "gpt-4o-mini", "quality": "gpt-4o", "json_mode": True,
    },
    "gemini": {
        "label": "Gemini (Google)", "protocol": "openai", "env": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "fast": "gemini-2.0-flash", "quality": "gemini-2.5-pro", "json_mode": True,
    },
    "groq": {
        "label": "Groq", "protocol": "openai", "env": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
        "fast": "llama-3.3-70b-versatile", "quality": "llama-3.3-70b-versatile", "json_mode": True,
    },
    "mistral": {
        "label": "Mistral", "protocol": "openai", "env": "MISTRAL_API_KEY",
        "base_url": "https://api.mistral.ai/v1",
        "fast": "mistral-small-latest", "quality": "mistral-large-latest", "json_mode": True,
    },
}
ORDER = ["deepseek", "anthropic", "openai", "gemini", "groq", "mistral"]

# Used when a model's price is not in the table: high on purpose, so the budget
# errs towards stopping. Shown in the settings as "price unknown".
UNKNOWN_PRICE = (3.0, 0.3, 15.0)

DEFAULTS = {"provider": "deepseek", "models": {}, "keys": {},
            "budget": {"per_request_usd": 0.02, "per_day_usd": 0.50}}

_lock = threading.Lock()


class AIError(Exception):
    """A failure worth showing to the user as it is: no key, over budget, the
    provider refused. Never carries a key."""


class NoKeyError(AIError):
    pass


class BudgetError(AIError):
    pass


# ── configuration ────────────────────────────────────────────────────────────

def load() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        cfg = {}
    out = json.loads(json.dumps(DEFAULTS))
    out.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    out["budget"] = {**DEFAULTS["budget"], **(cfg.get("budget") or {})}
    if out["provider"] not in PROVIDERS:
        out["provider"] = DEFAULTS["provider"]
    return out


def save(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with _lock:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        os.replace(tmp, CONFIG_PATH)


def key_for(cfg: dict, pid: str) -> tuple[str, str]:
    """(key, source). The saved key wins over the environment variable."""
    saved = (cfg.get("keys") or {}).get(pid, "")
    if saved:
        return saved, "settings"
    env = os.environ.get(PROVIDERS[pid]["env"], "")
    return (env, "environment") if env else ("", "")


def model_for(cfg: dict, pid: str, tier: str) -> str:
    return ((cfg.get("models") or {}).get(pid) or {}).get(tier) or PROVIDERS[pid][tier]


def price_for(pid: str, model: str) -> tuple[tuple[float, float, float], bool]:
    p = PROVIDERS[pid].get("prices", {}).get(model)
    return (p, True) if p else (UNKNOWN_PRICE, False)


def public_settings(db) -> dict:
    """Everything the Settings panel shows. No key values."""
    cfg = load()
    rows = []
    for pid in ORDER:
        p = PROVIDERS[pid]
        key, source = key_for(cfg, pid)
        fast, quality = model_for(cfg, pid, "fast"), model_for(cfg, pid, "quality")
        rows.append({
            "id": pid, "label": p["label"], "available": p.get("available", True),
            "note": p.get("note", ""), "env": p["env"],
            "fast": fast, "quality": quality,
            "defaultFast": p["fast"], "defaultQuality": p["quality"],
            "keySet": bool(key), "keySource": source, "keyHint": ("…" + key[-4:]) if key else "",
            "priceKnown": price_for(pid, fast)[1] and price_for(pid, quality)[1],
        })
    return {"provider": cfg["provider"], "providers": rows, "budget": cfg["budget"],
            "spentTodayUsd": round(spent_today(db), 5), "configPath": CONFIG_PATH}


def update_settings(body: dict) -> dict:
    """Apply a Settings form. A blank key field leaves the stored key alone;
    `clearKey` removes it."""
    cfg = load()
    pid = body.get("provider") or cfg["provider"]
    if pid not in PROVIDERS:
        raise AIError(f"unknown provider {pid!r}")
    cfg["provider"] = pid
    models = cfg.setdefault("models", {}).setdefault(pid, {})
    for tier in ("fast", "quality"):
        v = (body.get(tier) or "").strip()
        if v and re.fullmatch(r"[\w.\-:/@]{1,80}", v):
            models[tier] = v
        elif v == "" and tier in body:
            models.pop(tier, None)                  # back to the default
    key = (body.get("key") or "").strip()
    if body.get("clearKey"):
        cfg.setdefault("keys", {}).pop(pid, None)
    elif key:
        if len(key) < 8 or any(c.isspace() for c in key):
            raise AIError("that does not look like an API key")
        cfg.setdefault("keys", {})[pid] = key
    for name in ("per_request_usd", "per_day_usd"):
        if name in (body.get("budget") or {}):
            try:
                v = float(body["budget"][name])
            except (TypeError, ValueError):
                raise AIError(f"budget {name} must be a number")
            if not 0 <= v <= 100:
                raise AIError(f"budget {name} must be between 0 and 100 dollars")
            cfg["budget"][name] = v
    save(cfg)
    return cfg


# ── spending ─────────────────────────────────────────────────────────────────

def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def spent_today(db) -> float:
    r = db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM ai_usage WHERE day = ?",
                   (_today(),)).fetchone()
    return float(r[0] or 0)


def _record(db, pid, model, purpose, usage, cost):
    db.execute("""INSERT INTO ai_usage(at, day, provider, model, purpose, input_tokens,
                  cached_tokens, output_tokens, cost_usd) VALUES (?,?,?,?,?,?,?,?,?)""",
               (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), _today(),
                pid, model, purpose, usage[0], usage[1], usage[2], cost))
    db.commit()


def estimate_tokens(text: str) -> int:
    """Upper-bound token estimate without a tokenizer. Greek and other
    non-Latin text runs about one token per 1.5 characters, English about 4."""
    latin = sum(1 for c in text if ord(c) < 0x250)
    return int(latin / 3 + (len(text) - latin) / 1.3) + 8


def cost_of(pid: str, model: str, input_tokens: int, cached: int, output_tokens: int) -> float:
    (pin, pcache, pout), _ = price_for(pid, model)
    return (max(0, input_tokens - cached) * pin + cached * pcache + output_tokens * pout) / 1e6


def _check_budget(db, budget: dict, estimate: float) -> None:
    if estimate > budget["per_request_usd"]:
        raise BudgetError(f"This request could cost up to ${estimate:.4f}, over the "
                          f"per-request limit of ${budget['per_request_usd']:.2f}.")
    today = spent_today(db)
    if today + estimate > budget["per_day_usd"]:
        raise BudgetError(f"Today's AI spending is ${today:.4f}; this request could take it "
                          f"past the daily limit of ${budget['per_day_usd']:.2f}.")


# ── the call ─────────────────────────────────────────────────────────────────

def _post(url: str, key: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _error_text(e: urllib.error.HTTPError) -> str:
    try:
        j = json.loads(e.read().decode("utf-8", "replace"))
        msg = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else j.get("error")
        return str(msg or j)[:200]
    except Exception:
        return e.reason or ""


def parse_json(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        try:
            return json.loads(m.group(0)) if m else None
        except ValueError:
            return None


def chat_json(db, messages: list[dict], tier: str = "fast", max_tokens: int = 1200,
              purpose: str = "", timeout: float = 90.0, reasoning: bool = False) -> tuple[dict, dict]:
    """One JSON-returning call to the configured provider. Returns (data, meta)
    where meta has provider, model, tokens and cost. Raises AIError.

    Retries are for things a retry can fix, and never the same failing request
    twice: a busy or failing server (twice, with a pause), a reply cut off at
    max_tokens (once, with double the allowance, within budget), an empty reply
    (once), and a provider that rejects the reasoning switch (once, without it).
    """
    cfg = load()
    pid = cfg["provider"]
    p = PROVIDERS[pid]
    if not p.get("available", True):
        raise AIError(f"{p['label']} is not available yet: {p['note']}.")
    key, _ = key_for(cfg, pid)
    if not key:
        raise NoKeyError(f"No {p['label']} API key. Add one in Settings, or set {p['env']}.")
    model = model_for(cfg, pid, tier)
    budget = cfg["budget"]
    prompt_tokens = estimate_tokens("".join(m.get("content", "") for m in messages))
    _check_budget(db, budget, cost_of(pid, model, prompt_tokens, 0, max_tokens))

    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.2}
    if p.get("json_mode"):
        body["response_format"] = {"type": "json_object"}
    if not reasoning and p.get("quiet"):
        body.update(json.loads(json.dumps(p["quiet"])))
    url = p["base_url"].rstrip("/") + "/chat/completions"

    spent = 0.0
    transient = grew = empty = dropped_quiet = 0
    last = "no response"
    while True:
        try:
            resp = _post(url, key, body, timeout)
        except urllib.error.HTTPError as e:
            text = _error_text(e)
            last = f"{p['label']} returned HTTP {e.code}: {text}"
            quiet_keys = [k for k in (p.get("quiet") or {}) if k in body]
            if e.code == 400 and quiet_keys and not dropped_quiet:
                for k in quiet_keys:
                    body.pop(k)
                dropped_quiet = 1
                continue
            if (e.code == 429 or e.code >= 500) and transient < 2:
                transient += 1
                time.sleep(2 ** transient)
                continue
            raise AIError(last)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = f"Could not reach {p['label']}: {getattr(e, 'reason', e)}"
            if transient < 2:
                transient += 1
                time.sleep(1)
                continue
            raise AIError(last)

        u = resp.get("usage") or {}
        inp = int(u.get("prompt_tokens") or 0)
        cached = int(u.get("prompt_cache_hit_tokens")
                     or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        out = int(u.get("completion_tokens") or 0)
        thought = int((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        cost = cost_of(pid, model, inp, cached, out)
        spent += cost
        _record(db, pid, model, purpose, (inp, cached, out), cost)

        choice = (resp.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        data = parse_json(content)
        if isinstance(data, dict):
            return data, {"provider": pid, "model": model, "input_tokens": inp,
                          "cached_tokens": cached, "output_tokens": out,
                          "reasoning_tokens": thought, "cost_usd": round(spent, 6)}

        finish = choice.get("finish_reason")
        if finish == "length" and not grew:
            body["max_tokens"] *= 2
            _check_budget(db, budget, cost_of(pid, model, inp, 0, body["max_tokens"]))
            grew = 1
            continue
        if not content.strip() and not empty:
            # DeepSeek documents occasional empty content in JSON mode.
            empty = 1
            continue
        raise AIError(f"{p['label']} returned no usable JSON (finish: {finish}, "
                      f"{out} output tokens of {body['max_tokens']})")


def test_key(db) -> dict:
    """The cheapest possible real call, to prove the key and model work."""
    data, meta = chat_json(db, [
        {"role": "system", "content": "You answer in json."},
        {"role": "user", "content": 'Reply with exactly this json: {"ok": true}'},
    ], tier="fast", max_tokens=60, purpose="test")
    return {"ok": bool(data.get("ok")), **meta}

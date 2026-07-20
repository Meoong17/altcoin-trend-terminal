"""
News layer — per-coin sentiment & catalyst detection via CryptoPanic.

Why CryptoPanic and not homemade NLP: its posts are already TAGGED per
coin and carry community bullish/bearish VOTES. That makes sentiment a
measured aggregate of reader votes, not a keyword guess. This layer's
own keyword logic is used only for CATALYST FLAGGING (event-type
detection from headlines), where a fixed vocabulary is appropriate.

Honesty rules (consistent with the rest of the system):
  - Display-only layer. News sentiment does NOT feed trend_score or VaF
    automatically — an unvalidated sentiment signal must not contaminate
    the (also unvalidated, but at least deterministic) technical score.
    Its intended use: research input for the manual VaF metrics
    (catalyst_strength, catalyst_timing) in vaf_overrides.json.
  - Coins with no tagged news get null fields, never neutral-faked.
  - Requires CRYPTOPANIC_API_KEY (free at cryptopanic.com/developers/api).
    Absent key -> the layer is silently dormant.

Scoring:
  sentiment  -100..+100: sum of (positive - negative) votes per post,
             weighted by recency decay exp(-age_hours/24), squashed.
  catalysts  event flags from headline vocabulary, split positive
             (listing, mainnet, upgrade, ETF, partnership, buyback...)
             and negative (hack, exploit, lawsuit, delisting, unlock...).

Budget: currencies are batched ~40 codes/request; 400 coins = 10
requests per cycle, cached 3h -> well inside the free tier.
"""

import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

CP_BASE = "https://cryptopanic.com/api/v1/posts/"
CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          ".news_cache.json")
CACHE_TTL = 3 * 3600
BATCH = 40

CATALYSTS_POS = {
    "listing": r"\blist(s|ed|ing)?\b", "mainnet": r"\bmainnet\b",
    "upgrade": r"\bupgrade|hard ?fork\b", "etf": r"\betf\b",
    "partnership": r"\bpartner(s|ship)?\b", "launch": r"\blaunch(es|ed)?\b",
    "buyback": r"\bbuy ?back|burn(s|ed)?\b", "institutional": r"\binstitutional|blackrock|fidelity\b",
}
CATALYSTS_NEG = {
    "hack": r"\bhack(ed|er)?|exploit(ed)?\b", "lawsuit": r"\blawsuit|sues?|sec charge\b",
    "delisting": r"\bdelist(s|ed|ing)?\b", "unlock": r"\bunlock(s|ed)?\b",
    "outage": r"\boutage|halt(ed)?\b",
}


def _api_key():
    return os.environ.get("CRYPTOPANIC_API_KEY", "").strip() or None


def is_configured():
    return _api_key() is not None


# ── Pure, offline-testable transforms ──

def detect_catalysts(title):
    """Headline -> (positive_flags, negative_flags)."""
    t = (title or "").lower()
    pos = [k for k, pat in CATALYSTS_POS.items() if re.search(pat, t)]
    neg = [k for k, pat in CATALYSTS_NEG.items() if re.search(pat, t)]
    return pos, neg


def aggregate_posts(posts, now=None):
    """
    CryptoPanic post dicts -> {CODE: {sentiment, news_24h, catalysts_pos,
    catalysts_neg, top}}. Recency-decayed vote balance, squashed to
    -100..+100 via tanh so one viral post can't pin the scale.
    """
    now = now or datetime.now(timezone.utc)
    agg = {}
    for p in posts or []:
        title = p.get("title") or ""
        votes = p.get("votes") or {}
        try:
            published = datetime.fromisoformat(
                (p.get("published_at") or "").replace("Z", "+00:00"))
            age_h = max(0.0, (now - published).total_seconds() / 3600)
        except ValueError:
            age_h = 48.0
        w = math.exp(-age_h / 24.0)
        raw = (votes.get("positive") or 0) - (votes.get("negative") or 0)
        cpos, cneg = detect_catalysts(title)
        for cur in p.get("currencies") or []:
            code = (cur.get("code") or "").upper()
            if not code:
                continue
            a = agg.setdefault(code, {"_score": 0.0, "news_24h": 0,
                                      "catalysts_pos": set(), "catalysts_neg": set(),
                                      "top": None, "_top_w": -1})
            a["_score"] += raw * w
            if age_h <= 24:
                a["news_24h"] += 1
            a["catalysts_pos"].update(cpos)
            a["catalysts_neg"].update(cneg)
            imp = (votes.get("important") or 0) + abs(raw)
            if imp * w > a["_top_w"]:
                a["_top_w"] = imp * w
                a["top"] = {"title": title[:140], "url": p.get("url")}
    out = {}
    for code, a in agg.items():
        out[code] = {
            "sentiment": round(100 * math.tanh(a["_score"] / 10.0)),
            "news_24h": a["news_24h"],
            "catalysts_pos": sorted(a["catalysts_pos"]),
            "catalysts_neg": sorted(a["catalysts_neg"]),
            "top": a["top"],
        }
    return out


# ── Live fetch with cache ──

def fetch_news(symbols, now_ts=None):
    """
    {BinanceSymbol: news_dict} for symbols with tagged posts. Batched,
    cached 3h, per-batch failures logged and skipped.
    """
    if not is_configured():
        return {}
    now_ts = now_ts or time.time()
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH) as f:
                blob = json.load(f)
            if now_ts - blob.get("ts", 0) < CACHE_TTL:
                return blob.get("data", {})
        except (ValueError, OSError):
            pass

    codes = {}
    for s in symbols:
        base = s[:-4] if s.endswith("USDT") else s
        codes[base] = s
    code_list = sorted(codes)
    posts = []
    for i in range(0, len(code_list), BATCH):
        chunk = code_list[i:i + BATCH]
        try:
            r = requests.get(CP_BASE, params={
                "auth_token": _api_key(), "currencies": ",".join(chunk),
                "public": "true"}, timeout=20)
            r.raise_for_status()
            posts.extend((r.json() or {}).get("results") or [])
        except (requests.RequestException, ValueError) as e:
            print(f"[news] batch {i//BATCH} failed: {e}", file=sys.stderr)
        time.sleep(0.5)

    by_code = aggregate_posts(posts)
    data = {codes[c]: v for c, v in by_code.items() if c in codes}
    try:
        with open(CACHE_PATH, "w") as f:
            json.dump({"ts": now_ts, "data": data}, f)
    except OSError:
        pass
    return data

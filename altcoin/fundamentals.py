"""
Fundamental Intelligence layer — the free-tier-executable subset of the
sistem2 blueprint (tiered universe + DeFi fundamentals + Value Accrual).

Data source: DeFiLlama open API (api.llama.fi) — no key, no credits.
What it provides maps directly onto the blueprint:

    blueprint metric          DeFiLlama source
    ----------------          ----------------
    TVL + TVL growth          /protocol/{slug} tvl series
    Fees / Revenue            /summary/fees/{slug}?dataType=dailyFees|dailyRevenue
    Value Accrual             dataType=dailyHoldersRevenue — literally
                              "revenue that flows to token holders":
                              buybacks, burns, revenue share. The
                              blueprint's headline differentiator is a
                              first-class field here.
    P/F, P/S multiples        mcap (from the CoinStats listing already
                              fetched for the fallback layer) / annualized
                              fees & revenue

NOT implementable free-tier, deliberately absent (never silently faked):
    users/active wallets, developer activity, treasury, full
    unlock/emission schedules, meme-sentiment. Their composite weights
    are renormalized away — the same missing-data contract as GLF and
    score v2.

Composite weights follow the blueprint where the data exists:
    TVL 20% · Revenue 20% · Fee Growth 15% · Value Accrual 20%
    (users 10% / tokenomics 10% / treasury 5% -> renormalized out)

Caching: fundamentals move on daily timescales; a 12h file cache keeps
the polite-load promise to a free public API (same TTL as GLF).
"""

import json
import os
import sys
import time

import requests

LLAMA_BASE = "https://api.llama.fi"
CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          ".fundamentals_cache.json")
CACHE_TTL = 12 * 3600

# Binance symbol -> DeFiLlama parent slug. Curated like SYMBOL_GROUPS:
# deterministic, editable, and self-healing downstream (a wrong/renamed
# slug just logs and skips — the coin keeps its technical score).
DEFI_PROTOCOLS = {
    "AAVEUSDT": "aave",
    "UNIUSDT": "uniswap",
    "PENDLEUSDT": "pendle",
    "MORPHOUSDT": "morpho",
    "LDOUSDT": "lido",
    "CRVUSDT": "curve-dex",
    "COMPUSDT": "compound-finance",
    "GMXUSDT": "gmx",
    "DYDXUSDT": "dydx",
    "JUPUSDT": "jupiter",
    "RAYUSDT": "raydium",
    "CAKEUSDT": "pancakeswap",
    "SNXUSDT": "synthetix",
    "ENAUSDT": "ethena",
    "MKRUSDT": "sky",
}

WEIGHTS = {"tvl": 0.20, "revenue": 0.20, "fee_growth": 0.15, "value_accrual": 0.20}

ANNUALIZE_30D = 365.0 / 30.0


def _squash(x, scale):
    """Map x in [0, inf) onto 0-100 with diminishing returns; scale = the
    value that maps to ~50."""
    if x is None or x < 0:
        return None
    return 100.0 * x / (x + scale)


# ── Pure scoring (offline-testable) ──

def fundamental_scores(raw, mcap=None):
    """
    raw: {"tvl_now", "tvl_30d_ago", "fees_7d", "fees_30d",
          "revenue_30d", "holders_revenue_30d"}  (any may be None)
    -> (score 0-100 or None, details dict)

    Component definitions (documented, arbitrary-but-explicit — same
    epistemic status as score v2's technical weights):
      tvl:           30d TVL growth; +15% -> ~75, flat -> 50
      revenue:       annualized revenue yield on mcap (protocol
                     "earnings yield"); 2% -> ~50
      fee_growth:    weekly run-rate vs 30d run-rate; >1 accelerating
      value_accrual: holders_revenue / revenue — the fraction of protocol
                     earnings that actually reaches token holders. This is
                     the blueprint's star-rating, measured instead of
                     hand-assigned.
    """
    comps, det = {}, {}

    tvl_now, tvl_prev = raw.get("tvl_now"), raw.get("tvl_30d_ago")
    if tvl_now and tvl_prev:
        g = tvl_now / tvl_prev - 1
        det["tvl_growth_30d"] = round(g, 4)
        comps["tvl"] = max(0.0, min(100.0, 50 + g * 167))  # ±30% spans the scale

    rev30, mcap = raw.get("revenue_30d"), mcap
    if rev30 is not None and mcap:
        yield_ann = rev30 * ANNUALIZE_30D / mcap
        det["revenue_yield_ann"] = round(yield_ann, 4)
        comps["revenue"] = _squash(yield_ann, 0.02)

    f7, f30 = raw.get("fees_7d"), raw.get("fees_30d")
    if f7 is not None and f30:
        accel = (f7 / 7.0) / (f30 / 30.0)
        det["fee_accel_7v30"] = round(accel, 3)
        comps["fee_growth"] = max(0.0, min(100.0, 50 + (accel - 1) * 100))

    hrev, rev = raw.get("holders_revenue_30d"), raw.get("revenue_30d")
    if hrev is not None and rev:
        va = min(1.0, hrev / rev) if rev > 0 else 0.0
        det["value_accrual_ratio"] = round(va, 3)
        comps["value_accrual"] = 100.0 * va

    # Multiples for display (blueprint "DCF Crypto" section)
    if mcap and f30:
        det["price_to_fees"] = round(mcap / (f30 * ANNUALIZE_30D), 1)
    if mcap and rev30:
        det["price_to_revenue"] = round(mcap / (rev30 * ANNUALIZE_30D), 1)

    if not comps:
        return None, det
    tw = sum(WEIGHTS[k] for k in comps)
    score = sum(WEIGHTS[k] * v for k, v in comps.items()) / tw
    det["components"] = {k: round(v, 1) for k, v in comps.items()}
    det["missing"] = sorted(set(WEIGHTS) - set(comps))
    return round(score, 1), det


def blend_composite(technical_score, fundamental_score, w_technical=0.6):
    """
    Blueprint philosophy: fundamentals select, technicals time. The
    composite keeps both visible instead of gating hard, weighted toward
    the technical side because the fundamental layer's inputs are
    30d-slow. Returns None if either half is missing.
    """
    if technical_score is None or fundamental_score is None:
        return None
    return round(w_technical * technical_score +
                 (1 - w_technical) * fundamental_score, 1)


# ── Live fetch with cache ──

def _get(path, params=None):
    r = requests.get(f"{LLAMA_BASE}{path}", params=params or {}, timeout=25)
    r.raise_for_status()
    return r.json()


def _fees_summary(slug, data_type):
    try:
        d = _get(f"/summary/fees/{slug}", {"dataType": data_type})
        return {"total7d": d.get("total7d"), "total30d": d.get("total30d")}
    except (requests.RequestException, ValueError):
        return None


def _tvl_points(slug):
    try:
        d = _get(f"/protocol/{slug}")
        series = d.get("tvl") or []
        if len(series) < 31:
            return None, None
        return (float(series[-1].get("totalLiquidityUSD") or 0),
                float(series[-31].get("totalLiquidityUSD") or 0))
    except (requests.RequestException, ValueError, TypeError):
        return None, None


def fetch_fundamentals(symbols, mcaps=None, now=None):
    """
    {symbol: (score, details)} for symbols present in DEFI_PROTOCOLS.
    mcaps: optional {symbol: market_cap}. Cached 12h; per-slug failures
    log and skip so one bad slug never sinks the layer.
    """
    now = now or time.time()
    cache = {}
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH) as f:
                blob = json.load(f)
            if now - blob.get("ts", 0) < CACHE_TTL:
                cache = blob.get("data", {})
        except (ValueError, OSError):
            pass

    out = {}
    fresh = dict(cache)
    for symbol in symbols:
        slug = DEFI_PROTOCOLS.get(symbol)
        if not slug:
            continue
        if symbol in cache:
            raw = cache[symbol]
        else:
            fees = _fees_summary(slug, "dailyFees")
            rev = _fees_summary(slug, "dailyRevenue")
            hrev = _fees_summary(slug, "dailyHoldersRevenue")
            tvl_now, tvl_prev = _tvl_points(slug)
            if fees is None and tvl_now is None:
                print(f"[fundamentals] no data for {symbol} (slug '{slug}') — skipped",
                      file=sys.stderr)
                continue
            raw = {
                "tvl_now": tvl_now, "tvl_30d_ago": tvl_prev,
                "fees_7d": (fees or {}).get("total7d"),
                "fees_30d": (fees or {}).get("total30d"),
                "revenue_30d": (rev or {}).get("total30d"),
                "holders_revenue_30d": (hrev or {}).get("total30d"),
                "holders_revenue_7d": (hrev or {}).get("total7d"),
            }
            fresh[symbol] = raw
            time.sleep(0.3)  # polite pacing on a free public API
        score, det = fundamental_scores(raw, mcap=(mcaps or {}).get(symbol))
        if score is not None:
            out[symbol] = (score, det, raw)

    try:
        with open(CACHE_PATH, "w") as f:
            json.dump({"ts": now, "data": fresh}, f)
    except OSError:
        pass
    return out

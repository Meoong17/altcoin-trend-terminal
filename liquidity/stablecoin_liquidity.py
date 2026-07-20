"""
Stablecoin liquidity layer — the measurable subset of the v2.0 doc's
"Stablecoin Liquidity Score".

Doc formula: 0.35*Supply_Growth + 0.30*Net_Flow + 0.35*Exchange_Reserve.
Free-tier reality: Net_Flow and Exchange_Reserve_Ratio need exchange
wallet-labeling data (paid: Glassnode/CryptoQuant). Supply growth is
fully measurable from DeFiLlama's open stablecoin dataset — total
circulating USD, full daily history, no key.

So this module scores SUPPLY GROWTH only, renormalized to 100% of the
layer (the missing-data contract used everywhere else in this system),
and says so in its detail dict. Rising stablecoin float = dry powder
entering crypto; contracting float = liquidity leaving. It is the most
direct crypto-native liquidity gauge the free tier offers.

Score: 0-100, 50 = flat supply.
    growth_30d weighted 0.65, growth_7d (impulse) weighted 0.35;
    +2%/30d maps to ~70 — calibrated to the historical range where
    monthly stablecoin float moves of 2-3% accompanied major risk-on
    phases. Documented, arbitrary-but-explicit, same epistemic status
    as every other unvalidated weight here.

Endpoint: https://stablecoins.llama.fi/stablecoincharts/all
    -> [{"date": epoch_str, "totalCirculating": {"peggedUSD": x}, ...}]
Cached 12h.
"""

import json
import os
import sys
import time

import requests

STABLE_URL = "https://stablecoins.llama.fi/stablecoincharts/all"
CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          ".stablecoin_cache.json")
CACHE_TTL = 12 * 3600


def _extract_usd(row):
    for key in ("totalCirculatingUSD", "totalCirculating"):
        node = row.get(key)
        if isinstance(node, dict):
            v = node.get("peggedUSD")
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
    return None


def score_from_series(series):
    """
    Pure: ascending daily totals -> (score, detail) or (None, detail).
    Needs >= 31 points for the 30d leg; 7d impulse optional on top.
    """
    vals = [v for v in series if v]
    if len(vals) < 31:
        return None, {"status": "insufficient_history", "points": len(vals)}
    g30 = vals[-1] / vals[-31] - 1
    g7 = vals[-1] / vals[-8] - 1 if len(vals) >= 8 else None
    parts = [(0.65, 50 + g30 * 1000)]           # +2%/30d -> 70
    if g7 is not None:
        parts.append((0.35, 50 + g7 * 2500))    # +0.8%/7d -> 70 (impulse)
    tw = sum(w for w, _ in parts)
    score = max(0.0, min(100.0, sum(w * v for w, v in parts) / tw))
    return round(score, 1), {
        "status": "ok",
        "total_usd": round(vals[-1], 0),
        "growth_30d": round(g30, 4),
        "growth_7d": round(g7, 4) if g7 is not None else None,
        "coverage_note": "supply growth only; net-flow & exchange-reserve "
                         "legs need paid data and are excluded, not faked",
    }


def compute_stablecoin_liquidity(now=None):
    """(score, detail) with 12h file cache; (None, detail) on failure."""
    now = now or time.time()
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH) as f:
                blob = json.load(f)
            if now - blob.get("ts", 0) < CACHE_TTL:
                return blob["score"], blob["detail"]
        except (ValueError, OSError, KeyError):
            pass
    try:
        r = requests.get(STABLE_URL, timeout=25)
        r.raise_for_status()
        rows = r.json() or []
        series = [_extract_usd(row) for row in rows]
        score, detail = score_from_series(series)
    except (requests.RequestException, ValueError) as e:
        print(f"[stablecoin] fetch failed: {e}", file=sys.stderr)
        return None, {"status": "error", "error": str(e)}
    try:
        with open(CACHE_PATH, "w") as f:
            json.dump({"ts": now, "score": score, "detail": detail}, f)
    except OSError:
        pass
    return score, detail

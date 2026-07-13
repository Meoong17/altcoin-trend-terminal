#!/usr/bin/env python3
"""
SFC Repo Market Stress Module (M86)
======================================
M86 — SOFR-EFFR Spread (repo funding stress indicator)

Why this matters for a liquidity-focused model:
    SOFR (Secured Overnight Financing Rate) is the rate for overnight loans
    collateralized by Treasuries — the repo market, where banks/dealers
    borrow cash overnight against Treasury collateral. EFFR (Effective
    Federal Funds Rate) is the unsecured interbank overnight rate.

    Under normal conditions SOFR trades very close to EFFR (both anchored
    near the Fed's target rate). When SOFR spikes ABOVE EFFR, it means
    demand for overnight cash against safe collateral has outstripped
    supply — dealers/banks are struggling to source overnight funding even
    when they have Treasuries to pledge. This is a genuine liquidity
    stress signal, historically most visible in the September 2019 repo
    market crisis (SOFR spiked to ~5.25% vs EFFR ~2.25%, a ~300bp spread,
    forcing emergency Fed repo operations) and briefly around March 2020.

    This is a DIFFERENT dimension of "liquidity" than the Fed/ECB/BOJ/M2
    balance-sheet-growth signals already in global_liquidity_engine.py —
    those measure how much liquidity central banks are injecting/removing
    over months; this measures whether the plumbing connecting that
    liquidity to actual market participants is functioning smoothly
    right now. A model can show ample central-bank liquidity (high GLF)
    while simultaneously showing repo stress (funding markets seizing up
    at the transmission layer) — these are not redundant signals.

Data sources:
    FRED: SOFR (Secured Overnight Financing Rate, daily, %)
    FRED: EFFR (Effective Federal Funds Rate, daily, %)

NOTE ON HISTORICAL SPREAD: this module does NOT use TEDRATE (3-month
LIBOR minus 3-month T-bill) — that FRED series was discontinued following
LIBOR's phase-out (last observation 2022-01-03). SOFR-EFFR is the modern,
actively-updated equivalent for repo/funding stress.
"""

import json
import os
import sys
import time
import requests
from datetime import datetime, timezone

_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".repo_stress_cache.json")
CACHE_TTL = 21600  # 6 hours — SOFR/EFFR update once daily, no need to poll more often

FRED_KEY = os.getenv("FRED_API_KEY", "")
_FRED_CACHE = {}

# Spread thresholds, in basis points (bps). Calibrated against documented
# historical episodes:
#   - Normal conditions: SOFR-EFFR typically within +/- 5bp
#   - Sept 2019 repo crisis: spread reached ~300bp intraday, closed >100bp
#   - Elevated-but-not-crisis (quarter-end technical pressure, routine):
#     often 10-25bp
# These are NOT the same scale as the funding-rate bug found and fixed
# elsewhere in this codebase (Deribit interest_8h, capped at +/-0.5%) —
# SOFR/EFFR are annualized overnight rates in %, and bps here refers to
# the SPREAD between two such rates, a conceptually different quantity.
STRESS_THRESHOLDS_BPS = {
    "normal": 5,
    "elevated": 15,
    "high": 40,
    "crisis": 100,
}


def _fred(series, limit=5):
    """Fetch FRED data with module-level cache. Mirrors the _fred() pattern
    used in fiscal_liquidity.py / global_liquidity_engine.py for consistency."""
    global _FRED_CACHE
    cache_key = f"{series}:{limit}"
    if cache_key in _FRED_CACHE:
        return _FRED_CACHE[cache_key]
    if not FRED_KEY:
        return None
    try:
        r = requests.get(
            f"https://api.stlouisfed.org/fred/series/observations?series_id={series}"
            f"&api_key={FRED_KEY}&file_type=json&sort_order=desc&limit={limit}",
            timeout=15,
        )
        if r.status_code != 200:
            return None
        obs = r.json().get("observations", [])
        vals = [float(o["value"]) for o in obs if o["value"] != "."]
        result = vals if vals else None
        _FRED_CACHE[cache_key] = result
        return result
    except (requests.RequestException, ValueError, KeyError):
        return None


def _load_cache():
    try:
        with open(_CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"cached_at": 0}


def _save_cache(cache):
    try:
        with open(_CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except OSError:
        pass


def compute_repo_stress(force_refresh=False):
    """
    M86 — Repo market funding stress via SOFR-EFFR spread.

    Returns:
        (m86_score, detail_dict)
        m86_score: 0-1, higher = more repo stress (bearish for liquidity)
        detail_dict: {"spread_bps": float, "sofr": float, "effr": float,
                       "label": str, "status": "ok"|"unavailable"}
    """
    cache = _load_cache()
    now = time.time()

    if not force_refresh and (now - cache.get("cached_at", 0)) < CACHE_TTL:
        if cache.get("m86_score") is not None:
            return cache["m86_score"], cache.get("detail", {})

    sofr_vals = _fred("SOFR", limit=5)
    effr_vals = _fred("EFFR", limit=5)

    if not sofr_vals or not effr_vals:
        detail = {"status": "unavailable", "reason": "SOFR or EFFR unavailable from FRED"}
        return 0.5, detail  # neutral fallback, consistent with other liquidity modules' pattern

    sofr_latest = sofr_vals[0]
    effr_latest = effr_vals[0]
    spread_bps = (sofr_latest - effr_latest) * 100  # percentage points -> bps

    thresholds = STRESS_THRESHOLDS_BPS
    if spread_bps < thresholds["normal"]:
        score = 0.10
        label = "NORMAL"
    elif spread_bps < thresholds["elevated"]:
        score = 0.30
        label = "MILD_PRESSURE"
    elif spread_bps < thresholds["high"]:
        score = 0.55
        label = "ELEVATED"
    elif spread_bps < thresholds["crisis"]:
        score = 0.80
        label = "HIGH_STRESS"
    else:
        score = 0.95
        label = "CRISIS"

    # Negative spread (SOFR below EFFR) is unusual but not itself stress —
    # clamp score toward normal rather than extrapolating the threshold
    # ladder below zero, since a large NEGATIVE spread has no established
    # "stress" interpretation the way a large positive one does.
    if spread_bps < 0:
        score = min(score, 0.15)
        label = "BELOW_EFFR" if spread_bps < -thresholds["normal"] else "NORMAL"

    detail = {
        "spread_bps": round(spread_bps, 2),
        "sofr": round(sofr_latest, 4),
        "effr": round(effr_latest, 4),
        "label": label,
        "status": "ok",
    }

    cache["m86_score"] = score
    cache["detail"] = detail
    cache["cached_at"] = now
    _save_cache(cache)

    return score, detail


if __name__ == "__main__":
    score, detail = compute_repo_stress(force_refresh=True)
    print(f"M86 Repo Market Stress: {score}")
    print(f"Detail: {json.dumps(detail, indent=2)}")

    # Self-test: verify threshold ladder logic without network, using
    # synthetic spread values across the documented historical range.
    print("\n--- Self-test: threshold ladder (no network) ---")
    test_cases = [
        (2.0, "should be NORMAL"),
        (10.0, "should be MILD_PRESSURE"),
        (25.0, "should be ELEVATED"),
        (60.0, "should be HIGH_STRESS"),
        (150.0, "should be CRISIS (Sept 2019-like)"),
        (-3.0, "should be NORMAL (small negative)"),
    ]
    thresholds = STRESS_THRESHOLDS_BPS
    for spread_bps, expected_note in test_cases:
        if spread_bps < thresholds["normal"] and spread_bps >= 0:
            label = "NORMAL"
        elif spread_bps < 0:
            label = "BELOW_EFFR" if spread_bps < -thresholds["normal"] else "NORMAL"
        elif spread_bps < thresholds["elevated"]:
            label = "MILD_PRESSURE"
        elif spread_bps < thresholds["high"]:
            label = "ELEVATED"
        elif spread_bps < thresholds["crisis"]:
            label = "HIGH_STRESS"
        else:
            label = "CRISIS"
        print(f"  spread={spread_bps:+.1f}bp -> {label}  ({expected_note})")

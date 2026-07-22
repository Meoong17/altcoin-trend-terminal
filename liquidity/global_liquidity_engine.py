#!/usr/bin/env python3
"""
SFC Global Liquidity Engine (GLF — Global Liquidity Factor)
===========================================================
Consolidates ALL liquidity signals into ONE composite factor:

  Fed Balance Sheet (WALCL YoY)
  ECB Balance Sheet (ECBASSETSW YoY)
  PBOC/BOJ Balance Sheet (JPNASSETS YoY)
  TGA Balance (WTREGEN — fiscal liquidity drain)
  RRP Facility (RRPONTSYD — money market liquidity)
  DXY (USD Index — global dollar liquidity)
  US M2 Money Supply (M2SL YoY)
  Global GLO Index (from M33)

Output: GLF score 0–100 and SFC-mapped score 0–1
  High GLF (>70) = abundant liquidity = low stress (0.1–0.3)
  Low GLF (<30) = liquidity contraction = high stress (0.7–0.9)

Usage:
    from global_liquidity_engine import compute_global_liquidity_factor
    glf_score, glf_details = compute_global_liquidity_factor()
"""

import json, os, sys, math, time, requests
from datetime import datetime, timezone

SFC_DIR = os.path.dirname(os.path.abspath(__file__))
_CACHE_FILE = os.path.join(SFC_DIR, '.global_liquidity_cache.json')
CACHE_TTL = 43200  # 12 hours

FRED_KEY = os.getenv("FRED_API_KEY", "")
_FRED_CACHE = {}

def _fred(series, limit=13):
    """Fetch FRED data with module-level cache. Returns values only
    (unchanged contract); observation dates are cached alongside for
    callers that need a staleness check -- see _fred_latest_date()."""
    global _FRED_CACHE
    cache_key = f"{series}:{limit}"
    if cache_key in _FRED_CACHE:
        cached = _FRED_CACHE[cache_key]
        return cached[0] if isinstance(cached, tuple) else cached
    if not FRED_KEY:
        return None
    try:
        r = requests.get(
            f"https://api.stlouisfed.org/fred/series/observations?series_id={series}"
            f"&api_key={FRED_KEY}&file_type=json&sort_order=desc&limit={limit}",
            timeout=15
        )
        if r.status_code != 200:
            return None
        obs = r.json().get("observations", [])
        vals = [float(o["value"]) for o in obs if o["value"] != "."]
        latest_date = obs[0]["date"] if obs else None
        result = vals if vals else None
        _FRED_CACHE[cache_key] = (result, latest_date)
        return result
    except:
        return None


def _fred_latest_date(series, limit=13):
    """Date string (YYYY-MM-DD) of the most recent observation for a
    series already fetched via _fred() this cycle, or None. Reads the
    same cache entry _fred() populates -- call _fred() first."""
    cached = _FRED_CACHE.get(f"{series}:{limit}")
    if isinstance(cached, tuple):
        return cached[1]
    return None


def _is_stale(date_str, max_age_days=120):
    """True if a FRED observation date is older than max_age_days -- the
    guard that was missing for China M2 (see audit note below). 120 days
    tolerates normal monthly-release lag; a series frozen for longer than
    that is treated as unavailable, never silently used as if current."""
    if not date_str:
        return True
    try:
        obs_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - obs_date).days > max_age_days


def _load_cache():
    try:
        with open(_CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"cached_at": 0}

def _save_cache(cache):
    cache["cached_at"] = time.time()
    with open(_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def _compute_dxy():
    """Compute DXY from exchange rates (same as collect.py get_dxy)."""
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
        if r.status_code != 200:
            raise ValueError(f"HTTP {r.status_code}")
        rates = r.json().get("rates", {})
        eur = 1.0 / rates["EUR"]
        jpy = rates["JPY"]
        gbp = 1.0 / rates["GBP"]
        cad = rates["CAD"]
        sek = rates["SEK"]
        chf = rates["CHF"]
        dxy = 50.14348112 * (eur ** -0.576) * (jpy ** 0.136) * (gbp ** -0.119) * (cad ** 0.091) * (sek ** 0.042) * (chf ** 0.036)
        return round(dxy, 2)
    except:
        return None


def _fetch_all_parallel():
    """Fetch all FRED series + DXY in parallel batch."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    series_needed = [
        ("WALCL", 13),       # Fed balance sheet
        ("ECBASSETSW", 13),  # ECB balance sheet
        ("JPNASSETS", 13),   # BOJ balance sheet
        ("WTREGEN", 8),      # TGA balance
        ("RRPONTSYD", 8),    # RRP facility
        ("M2SL", 13),        # M2 money supply
        ("MYAGM2CNM189N", 13),  # China M2 (Broad Money, OECD via FRED) — see
                                 # note in _compute_china_component() below;
                                 # UNVERIFIED series ID, confirm on FRED before relying on this.
    ]

    def _fetch_one(series, limit):
        vals = _fred(series, limit)
        return series, vals

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_fetch_one, s, l): s for s, l in series_needed}
        for f in as_completed(futures):
            pass  # _fred stores in cache

    # DXY
    dxy = _compute_dxy()
    if dxy is not None:
        _FRED_CACHE["DXY"] = dxy

    return True


def _yoy_chg(arr):
    """Compute YoY % change from array [latest, ..., oldest]."""
    if not arr or len(arr) < 13:
        return None
    if arr[12] == 0:
        return 0
    return (arr[0] - arr[12]) / arr[12] * 100


def _z_score(value, mean, std):
    """Compute z-score, clip to [-3, 3]."""
    if std == 0 or value is None:
        return 0
    return max(-3.0, min(3.0, (value - mean) / std))


def compute_global_liquidity_factor(force_refresh=False):
    """
    Compute Global Liquidity Factor (GLF) — the single most important
    macro signal for SFC.

    Components and weights (based on market impact):
        Fed Balance Sheet YoY:      30%  (largest CB, directly drives risk assets)
        ECB Balance Sheet YoY:      15%
        BOJ Balance Sheet YoY:       3%  (reduced from 5% — China split out below)
        China M2 YoY:                4%  (NEW — was previously mislabeled as
                                           part of "BOJ/PBOC", never actually
                                           fetched; see _compute china_z note)
        US M2 YoY:                  15%
        TGA Composite:              10%  (fiscal liquidity drain)
        RRP Composite:              10%  (money market liquidity)
        DXY (inverted):             13%  (reduced from 15%; USD strength = liquidity tightening)

    Returns:
        (glf_score_0_100, sfc_stress_0_1, details_dict)
        glf_score_0_100: 0=extreme liquidity contraction, 100=abundant liquidity
        sfc_stress_0_1:  high = stress (for SFC pipeline consumption)
    """
    now = time.time()
    cache = _load_cache()

    # Check cache
    if not force_refresh and (now - cache.get("cached_at", 0)) < CACHE_TTL:
        if cache.get("glf_score") is not None:
            return cache["glf_score"], cache["sfc_stress"], cache.get("details", {})

    # Fetch all data in parallel
    _fetch_all_parallel()

    # ── 1. Fed Balance Sheet YoY ──
    walcl = _FRED_CACHE.get("WALCL:13")
    fed_yoy = _yoy_chg(walcl) if walcl else None
    # Normalize: historical mean ~5.5%, std ~8%
    # Positive = expansion (bullish for liquidity)
    fed_z = _z_score(fed_yoy, 5.5, 8.0) if fed_yoy is not None else 0

    # ── 2. ECB Balance Sheet YoY ──
    ecb = _FRED_CACHE.get("ECBASSETSW:13")
    ecb_yoy = _yoy_chg(ecb) if ecb else None
    ecb_z = _z_score(ecb_yoy, 4.0, 7.0) if ecb_yoy is not None else 0

    # ── 3. BOJ Balance Sheet YoY ──
    jpn = _FRED_CACHE.get("JPNASSETS:13")
    jpn_yoy = _yoy_chg(jpn) if jpn else None
    jpn_z = _z_score(jpn_yoy, 3.0, 6.0) if jpn_yoy is not None else 0

    # ── 3b. China M2 YoY ──
    # NOTE ON SERIES ID: MYAGM2CNM189N (China Broad Money, OECD via FRED)
    # was NOT verified against a live FRED query when this was written —
    # network access was unavailable in the environment this was built in.
    # Confirm the series resolves at
    # https://fred.stlouisfed.org/series/MYAGM2CNM189N before relying on
    # this component; if it 404s or returns no data, china_yoy will
    # correctly come back None and this component contributes 0 to GLF
    # (fails safe, doesn't silently corrupt the score) — but you should
    # still find the correct series ID and update it here, since a
    # permanently-zero component just wastes its weight allocation.
    #
    # This was added because the previous "BOJ/PBOC Balance Sheet" label
    # was misleading: the code only ever fetched JPNASSETS (Bank of Japan)
    # — no PBOC/China-specific data was ever fetched despite the comment
    # implying otherwise. China is a genuinely separate liquidity engine
    # from Japan; conflating them under one series was inaccurate for a
    # liquidity-focused model.
    china = _FRED_CACHE.get("MYAGM2CNM189N:13")
    china = china[0] if isinstance(china, tuple) else china
    china_date = _fred_latest_date("MYAGM2CNM189N")
    if china and _is_stale(china_date):
        # The date-guard flagged as PENDING since the first audit: this
        # series has previously been observed frozen for long stretches.
        # A frozen series still returns non-None values every cycle, so
        # without this check a stale YoY figure would silently feed GLF
        # forever with no degraded flag anywhere -- the one GLF component
        # that had escaped the "missing/stale -> None" discipline every
        # other layer in this system follows.
        print(f"[GLF] China M2 (MYAGM2CNM189N) stale as of {china_date} — excluded, not faked",
              file=sys.stderr)
        china = None
    china_yoy = _yoy_chg(china) if china else None
    # Mean/std here are a rough starting estimate (China M2 growth has
    # historically run higher than US/EU, often 8-12%/yr) — NOT yet
    # verified against real FRED history the way liquidity_zscore_calibration.py
    # does for the other four components. Run that script against this
    # series once the ID is confirmed working.
    china_z = _z_score(china_yoy, 9.0, 3.5) if china_yoy is not None else 0

    # ── 4. US M2 YoY ──
    m2 = _FRED_CACHE.get("M2SL:13")
    m2_yoy = _yoy_chg(m2) if m2 else None
    m2_z = _z_score(m2_yoy, 6.0, 4.0) if m2_yoy is not None else 0

    # ── 5. TGA Balance ──
    tga_vals = _FRED_CACHE.get("WTREGEN:8")
    tga_score = 0
    tga_detail = "unavailable"
    if tga_vals and len(tga_vals) >= 2:
        tga_latest = tga_vals[0]
        tga_chg = (tga_vals[0] - tga_vals[3]) / tga_vals[3] * 100 if len(tga_vals) >= 4 and tga_vals[3] > 0 else 0
        # TGA decreasing = stimulus (bullish for liquidity)
        # TGA increasing = drain (bearish for liquidity)
        if tga_chg < -10:
            tga_z = 1.5   # strong stimulus
        elif tga_chg < -5:
            tga_z = 0.8
        elif tga_chg < -2:
            tga_z = 0.3
        elif tga_chg < 2:
            tga_z = 0.0   # neutral
        elif tga_chg < 5:
            tga_z = -0.5
        elif tga_chg < 10:
            tga_z = -1.0
        else:
            tga_z = -1.5  # strong drain
        # Absolute level adjustment
        if tga_latest > 900000:  # >$900B — high drain risk
            tga_z -= 0.5
        elif tga_latest < 300000:  # <$300B — rebuilding risk
            tga_z -= 0.3
        tga_z = max(-2.0, min(2.0, tga_z))
        tga_score = tga_z
        tga_detail = {
            "tga_b": round(tga_latest / 1000, 1),
            "tga_chg_pct": round(tga_chg, 2),
            "tga_z": round(tga_z, 3),
        }
    else:
        tga_score = 0

    # ── 6. RRP Facility ──
    rrp_vals = _FRED_CACHE.get("RRPONTSYD:8")
    rrp_score = 0
    rrp_detail = "unavailable"
    if rrp_vals and len(rrp_vals) >= 2:
        rrp_latest = rrp_vals[0]
        rrp_chg = rrp_vals[0] - rrp_vals[3] if len(rrp_vals) >= 4 else 0
        # RRP low/near zero = cash deployed = bullish
        # RRP high = cash parked = bearish
        if rrp_latest < 10:
            rrp_z = 1.5   # near zero — all cash deployed
        elif rrp_latest < 50:
            rrp_z = 1.0
        elif rrp_latest < 200:
            rrp_z = 0.0   # moderate
        elif rrp_latest < 500:
            rrp_z = -0.5
        else:
            rrp_z = -1.5  # massive cash parked
        # Trend adjustment
        if rrp_chg < -50:
            rrp_z += 0.5
        elif rrp_chg > 50:
            rrp_z -= 0.5
        rrp_z = max(-2.0, min(2.0, rrp_z))
        rrp_score = rrp_z
        rrp_detail = {
            "rrp_b": round(rrp_latest, 1),
            "rrp_chg_b": round(rrp_chg, 1),
            "rrp_z": round(rrp_z, 3),
        }
    else:
        rrp_score = 0

    # ── 7. DXY (inverted: high DXY = tight dollar liquidity) ──
    dxy = _FRED_CACHE.get("DXY")
    dxy_z = 0
    if dxy is not None:
        # DXY ~100 is neutral. <95 = weak USD (bullish liquidity).
        # >105 = strong USD (bearish liquidity).
        dxy_z = _z_score(dxy, 100, 5.0) * -1  # invert: high DXY = negative
        dxy_z = max(-2.0, min(2.0, dxy_z))

    # ── Compute weighted GLF score ──
    # Structure: {component: (z_score, weight)}
    components = {}

    if fed_yoy is not None:
        components["fed"] = (fed_z, 0.30)
    if ecb_yoy is not None:
        components["ecb"] = (ecb_z, 0.15)
    if jpn_yoy is not None:
        components["jpn"] = (jpn_z, 0.03)  # reduced from 0.05 to make room for china (separate component below)
    if china_yoy is not None:
        components["china"] = (china_z, 0.04)  # NEW — see note above on unverified series ID
    if m2_yoy is not None:
        components["m2"] = (m2_z, 0.15)
    if isinstance(tga_detail, dict):
        components["tga"] = (tga_score, 0.10)
    if isinstance(rrp_detail, dict):
        components["rrp"] = (rrp_score, 0.10)
    if dxy is not None:
        components["dxy"] = (dxy_z, 0.13)  # reduced from 0.15 to make room for china component

    if not components:
        # Fallback: neutral
        return 50.0, 0.50, {"error": "no data available", "status": "fallback"}

    total_w = sum(w for _, w in components.values())
    glf_z = sum(z * w for z, w in components.values()) / total_w

    # Map z-score to GLF 0–100
    # z=+2 (very liquid) → GLF=90
    # z=0 (neutral)     → GLF=55
    # z=-2 (tight)      → GLF=10
    glf_raw = 55 + glf_z * 17.5
    glf_score = max(0, min(100, glf_raw))

    # Map to SFC stress score (0–1, high = stress)
    if glf_score > 70:
        sfc_stress = 0.15   # abundant liquidity
    elif glf_score > 55:
        sfc_stress = 0.30   # moderately liquid
    elif glf_score > 40:
        sfc_stress = 0.50   # neutral
    elif glf_score > 25:
        sfc_stress = 0.70   # contracting
    else:
        sfc_stress = 0.85   # severe contraction

    # Regime label
    if glf_score > 70:
        regime = "ABUNDANT_LIQUIDITY"
    elif glf_score > 55:
        regime = "ACCOMMODATIVE"
    elif glf_score > 40:
        regime = "NEUTRAL"
    elif glf_score > 25:
        regime = "TIGHTENING"
    else:
        regime = "LIQUIDITY_CRISIS"

    # Component details for debugging
    comp_detail = {}
    for name, (z_val, weight) in components.items():
        raw_val = None
        if name == "fed":
            raw_val = round(fed_yoy, 2) if fed_yoy is not None else None
        elif name == "ecb":
            raw_val = round(ecb_yoy, 2) if ecb_yoy is not None else None
        elif name == "jpn":
            raw_val = round(jpn_yoy, 2) if jpn_yoy is not None else None
        elif name == "china":
            raw_val = round(china_yoy, 2) if china_yoy is not None else None
        elif name == "m2":
            raw_val = round(m2_yoy, 2) if m2_yoy is not None else None
        elif name == "tga" and isinstance(tga_detail, dict):
            raw_val = tga_detail.get("tga_chg_pct")
        elif name == "rrp" and isinstance(rrp_detail, dict):
            raw_val = rrp_detail.get("rrp_b")
        elif name == "dxy":
            raw_val = round(dxy, 2) if dxy is not None else None
        comp_detail[name] = {
            "z_score": round(z_val, 3),
            "weight": weight,
            "raw": raw_val,
        }

    details = {
        "glf_score": round(glf_score, 1),
        "glf_z_score": round(glf_z, 3),
        "sfc_stress": round(sfc_stress, 3),
        "regime": regime,
        "components": comp_detail,
        "dxy": round(dxy, 2) if dxy is not None else None,
        "tga": tga_detail,
        "rrp": rrp_detail,
        "active_components": len(components),
        "n_components": len(components),
        "status": "ok",
    }

    # Save to cache
    cache["glf_score"] = glf_score
    cache["sfc_stress"] = sfc_stress
    cache["details"] = details
    _save_cache(cache)

    return glf_score, round(sfc_stress, 3), details


def get_glf_for_factors(glf_sfc_stress=None):
    """
    Convert GLF sfc_stress to factor adjustment for Lt (Liquidity) factor.
    GLF is already in SFC stress convention (high = stress).

    Returns adjustment value in [-2.0, +2.0] range for Lt factor.
    """
    if glf_sfc_stress is None:
        return 0.0
    # Map sfc_stress 0-1 to Lt adjustment
    # sfc_stress=0.15 (very liquid) → adj=+1.5 (bullish)
    # sfc_stress=0.50 (neutral)    → adj=0.0
    # sfc_stress=0.85 (crisis)     → adj=-1.5 (bearish)
    adj = (0.50 - glf_sfc_stress) * 3.0
    return max(-2.0, min(2.0, adj))


def get_glf_weight_by_regime(regime_name="NORMAL"):
    """
    Return GLF weight for the 5-factor model based on market regime.
    In normal times, liquidity matters ~35%.
    In crisis, liquidity matters more (~50%).
    In bull markets, slightly less (~25%).
    """
    weights = {
        "BULL":      0.25,
        "BEAR":      0.40,
        "SIDEWAYS":  0.35,
        "CRISIS":    0.50,
        "NORMAL":    0.35,
        "STRESS":    0.45,
        "CAPITULATION": 0.50,
    }
    return weights.get(regime_name.upper(), 0.35)


if __name__ == "__main__":
    glf, sfc_stress, details = compute_global_liquidity_factor(force_refresh=True)
    print(json.dumps({
        "glf_score": glf,
        "sfc_stress": sfc_stress,
        "regime": details.get("regime"),
        "active_components": details.get("active_components"),
        "details": details,
    }, indent=2))

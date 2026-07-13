"""
Market/technical feature engineering — the free-tier-compatible subset of
the Altcoin Trend Detection blueprint (section 2.2A "Fitur Pasar").

Implemented from the blueprint:
    - Relative Strength vs BTC:  RS_7d and normalized IR_7d
    - Breakout Score (0-100): proximity to 30d high, RSI ideal zone
      (55-68), volume expansion percentile, ATR expansion
    - Volatility Compression: Bollinger Band width percentile (90d) low
      + early volume pickup (2d ratio)
    - Volume Percentile (90d)
    - Trend Consistency: green-day ratio (30d), EMA20/EMA50 slope + streak
    - Rolling 30d correlation vs BTC

Deliberately NOT implemented (blueprint sections that need paid data or
ML infra that doesn't exist yet): on-chain features (Glassnode is paid,
coverage thin outside BTC/ETH), OI/funding/CVD (Coinglass paid), the HMM
regime model (see regime.py for the deterministic proxy), and the ML
ensemble/calibration layers (blocked on accumulating training history —
see history.py).

Every function here is pure (lists in, numbers out) so the offline
self-test in analyzer.py can exercise the math without network access.
All "percentile" features compare the CURRENT value against its own
trailing 90-day distribution — a single rank, which is what the
composite score needs at inference time.
"""


def pct_rank(value, history):
    """Percentile rank (0-100) of value within history. None-safe."""
    if value is None or not history:
        return None
    below = sum(1 for h in history if h <= value)
    return 100.0 * below / len(history)


def ema(closes, span):
    if len(closes) < span:
        return None
    k = 2.0 / (span + 1)
    e = sum(closes[:span]) / span
    for c in closes[span:]:
        e = c * k + e * (1 - k)
    return e


def atr_series(highs, lows, closes, period=14):
    """Wilder ATR series; returns [] if not enough data."""
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return []
    trs = []
    for i in range(1, n):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    atr = sum(trs[:period]) / period
    out = [atr]
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
        out.append(atr)
    return out


def bb_width_series(closes, period=20, k=2.0):
    """Bollinger Band width (relative to middle band) per bar."""
    out = []
    for i in range(period, len(closes) + 1):
        win = closes[i - period:i]
        m = sum(win) / period
        var = sum((c - m) ** 2 for c in win) / period
        sd = var ** 0.5
        out.append((2 * k * sd) / m if m else 0.0)
    return out


def returns(closes, horizon=1):
    return [closes[i] / closes[i - horizon] - 1
            for i in range(horizon, len(closes))]


def rs_vs_btc(closes, btc_closes, horizon=7, norm_window=30):
    """
    Blueprint: RS_7d = ret_alt_7d - ret_btc_7d;
    IR_7d = RS_7d / std(RS_7d over rolling 30d).
    Series are tail-aligned (both end at the latest candle).
    Returns (rs_7d, ir_7d) or (None, None).
    """
    n = min(len(closes), len(btc_closes))
    need = horizon + norm_window
    if n < need + 1:
        return None, None
    a, b = closes[-n:], btc_closes[-n:]
    rs_series = [
        (a[i] / a[i - horizon] - 1) - (b[i] / b[i - horizon] - 1)
        for i in range(horizon, n)
    ]
    rs_now = rs_series[-1]
    window = rs_series[-norm_window:]
    m = sum(window) / len(window)
    sd = (sum((x - m) ** 2 for x in window) / len(window)) ** 0.5
    return rs_now, (rs_now / sd if sd > 1e-12 else None)


def corr_vs_btc(closes, btc_closes, window=30):
    """Rolling correlation of daily returns over the last `window` days."""
    n = min(len(closes), len(btc_closes))
    if n < window + 1:
        return None
    ra = returns(closes[-(window + 1):])
    rb = returns(btc_closes[-(window + 1):])
    ma, mb = sum(ra) / window, sum(rb) / window
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((y - mb) ** 2 for y in rb)
    if va <= 1e-18 or vb <= 1e-18:
        return None
    return cov / (va ** 0.5 * vb ** 0.5)


def rsi_zone_score(rsi, lo=55, hi=68, decay=12.0):
    """
    Blueprint's breakout RSI term: 1.0 inside the 55-68 "ideal
    momentum" zone, decaying linearly to 0 within `decay` points
    outside it. Returns 0-1.
    """
    if rsi is None:
        return None
    if lo <= rsi <= hi:
        return 1.0
    dist = (lo - rsi) if rsi < lo else (rsi - hi)
    return max(0.0, 1.0 - dist / decay)


def green_day_ratio(closes, window=30):
    if len(closes) < window + 1:
        return None
    rets = returns(closes[-(window + 1):])
    return sum(1 for r in rets if r > 0) / len(rets)


def ema_trend(closes, fast=20, slow=50):
    """
    Blueprint trend-consistency term: (EMA20-EMA50)/EMA50 slope, plus
    the streak of consecutive days that spread has been positive.
    Returns (slope, streak_days) or (None, None).
    """
    if len(closes) < slow + 2:
        return None, None
    spreads = []
    for i in range(slow, len(closes) + 1):
        f, s = ema(closes[:i], fast), ema(closes[:i], slow)
        spreads.append((f - s) / s if s else 0.0)
    streak = 0
    for v in reversed(spreads):
        if v > 0:
            streak += 1
        else:
            break
    return spreads[-1], streak


def compute_feature_set(highs, lows, closes, quote_volumes, btc_closes):
    """
    Full §2.2A feature dict for one coin. Volumes are CLOSED candles
    (caller already dropped the in-progress one). Any feature whose
    lookback exceeds the available history is None — never silently 0
    (the sfc-terminal audit lesson).
    """
    f = {}
    # Relative strength
    f["rs_7d"], f["ir_7d"] = rs_vs_btc(closes, btc_closes) if btc_closes else (None, None)
    f["corr_btc_30d"] = corr_vs_btc(closes, btc_closes) if btc_closes else None

    # Breakout block
    if len(closes) >= 30:
        lo30, hi30 = min(closes[-30:]), max(closes[-30:])
        f["prox_30d_high"] = ((closes[-1] - lo30) / (hi30 - lo30)) if hi30 > lo30 else None
    else:
        f["prox_30d_high"] = None

    vols = quote_volumes or []
    f["vol_pct_90d"] = pct_rank(vols[-1], vols[-91:-1]) if len(vols) >= 31 else None
    f["vol_2d_ratio"] = (sum(vols[-2:]) / 2) / (sum(vols[-30:]) / len(vols[-30:])) \
        if len(vols) >= 30 and sum(vols[-30:]) > 0 else None

    atrs = atr_series(highs, lows, closes)
    if len(atrs) >= 31:
        f["atr_expansion"] = atrs[-1] / (sum(atrs[-31:-1]) / 30) if sum(atrs[-31:-1]) else None
        f["atr_pct_90d"] = pct_rank(atrs[-1], atrs[-91:-1])
    else:
        f["atr_expansion"] = f["atr_pct_90d"] = None

    # Volatility compression
    bbw = bb_width_series(closes)
    f["bbw_pct_90d"] = pct_rank(bbw[-1], bbw[-91:-1]) if len(bbw) >= 31 else None
    f["compression_setup"] = (
        f["bbw_pct_90d"] is not None and f["vol_2d_ratio"] is not None
        and f["bbw_pct_90d"] < 20 and f["vol_2d_ratio"] > 1.5
    ) if (f["bbw_pct_90d"] is not None and f["vol_2d_ratio"] is not None) else None

    # Trend consistency
    f["green_ratio_30d"] = green_day_ratio(closes)
    f["ema_slope"], f["ema_streak"] = ema_trend(closes)
    return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in f.items()}


# ── Composite score v2 with explainable drivers (blueprint §6 shape) ──

WEIGHTS = {
    "breakout": 0.30,
    "rel_strength": 0.30,
    "trend_consistency": 0.20,
    "compression": 0.10,
    "macro": 0.10,
}


def _clip01(x):
    return max(0.0, min(1.0, x))


def score_components(f, rsi, macro_component):
    """
    Each component scored 0-100 from the feature dict; None components
    are excluded and remaining weights renormalized (same missing-data
    contract as the GLF engine). Returns (score, drivers) where drivers
    is a list of {component, value, weight, contribution} sorted by
    |contribution - neutral|, mimicking the blueprint's "Top Drivers"
    output without needing SHAP.
    """
    comps = {}

    parts = []
    if f.get("prox_30d_high") is not None:
        parts.append((0.40, 100 * _clip01(f["prox_30d_high"])))
    z = rsi_zone_score(rsi)
    if z is not None:
        parts.append((0.20, 100 * z))
    if f.get("vol_pct_90d") is not None:
        parts.append((0.25, f["vol_pct_90d"]))
    if f.get("atr_expansion") is not None:
        parts.append((0.15, 100 * _clip01((f["atr_expansion"] - 0.8) / 0.8)))
    if parts:
        tw = sum(w for w, _ in parts)
        comps["breakout"] = sum(w * v for w, v in parts) / tw

    if f.get("ir_7d") is not None:
        comps["rel_strength"] = 100 * _clip01(0.5 + f["ir_7d"] / 4.0)  # IR ±2σ spans the scale
    elif f.get("rs_7d") is not None:
        comps["rel_strength"] = 100 * _clip01(0.5 + f["rs_7d"] / 0.2)

    tparts = []
    if f.get("green_ratio_30d") is not None:
        tparts.append((0.5, 100 * f["green_ratio_30d"]))
    if f.get("ema_slope") is not None:
        slope_score = _clip01(0.5 + f["ema_slope"] / 0.10)
        streak_bonus = _clip01((f.get("ema_streak") or 0) / 20.0)
        tparts.append((0.5, 100 * (0.7 * slope_score + 0.3 * streak_bonus)))
    if tparts:
        tw = sum(w for w, _ in tparts)
        comps["trend_consistency"] = sum(w * v for w, v in tparts) / tw

    if f.get("compression_setup") is not None:
        comps["compression"] = 85.0 if f["compression_setup"] else (
            60.0 if (f.get("bbw_pct_90d") is not None and f["bbw_pct_90d"] < 20) else 40.0)

    if macro_component is not None:
        comps["macro"] = max(0.0, min(100.0, macro_component))

    if not comps:
        return None, []

    total_w = sum(WEIGHTS[k] for k in comps)
    score = sum(WEIGHTS[k] * v for k, v in comps.items()) / total_w
    drivers = [
        {
            "component": k,
            "value": round(v, 1),
            "weight": WEIGHTS[k],
            "contribution": round((v - 50.0) * WEIGHTS[k] / total_w, 2),
        }
        for k, v in comps.items()
    ]
    drivers.sort(key=lambda d: -abs(d["contribution"]))
    return round(score, 1), drivers

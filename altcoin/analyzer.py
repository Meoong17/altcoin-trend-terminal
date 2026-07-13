"""
altcoin/analyzer.py — Generic per-coin trend analyzer (multi-coin capable)
=============================================================================

Parameterized by `symbol` (any Binance-listed pair, e.g. "ETHUSDT",
"SOLUSDT", "BNBUSDT") — the same functions work for any coin without
code duplication. Binance's public REST API requires no key and supports
any listed symbol with an identical request shape, so adding a new coin
to track is a config change (add to TRACKED_SYMBOLS in collect.py), not a
new code path.

What's coin-specific here (computed fresh per symbol):
    - Price history, RSI, momentum (technical indicators)
    - Ratio vs BTC (classic "altseason" signal — rising ETH/BTC or
      SOL/BTC means capital is rotating from BTC into that alt, a risk-on
      signal within crypto specifically, distinct from risk-on/off vs
      traditional markets)

What's explicitly NOT duplicated per coin (see collect.py):
    - Global Liquidity Factor (GLF) and Repo Market Stress — both are
      macro signals independent of which crypto asset you're looking at.
      Computing these once and reusing across all tracked coins avoids
      redundant FRED API calls and keeps the "why does ETH's score differ
      from SOL's" answer honest: only the coin-specific technical layer
      differs, not two different (and potentially drifting) copies of the
      same macro data.
"""
import os
import sys
import time

import requests


def _compute_rvm(closes):
    """Return/volatility/momentum from a list of closes (oldest first).
    Same implementation as market_data_fetcher.py's _compute_rvm — kept
    duplicated here rather than imported cross-repo, since this is
    intentionally a separate, standalone repo (see docs/ARCHITECTURE.md)."""
    if not closes or len(closes) < 8:
        return None
    returns = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(1, len(closes)) if closes[i - 1]
    ]
    if len(returns) < 7:
        return None
    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / len(returns)
    return {
        "return": round(returns[-1], 6),
        "volatility": round(variance ** 0.5, 6),
        "momentum": round(sum(returns[-3:]) / 3 - sum(returns[-7:]) / 7, 6),
    }


def _compute_rsi(closes, period=14):
    """Standard RSI-14. Returns None if insufficient data."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _compute_volume_metrics(quote_volumes):
    """
    Volume indicator from daily quote volumes (oldest-first, USDT).

    IMPORTANT — incomplete-candle handling: Binance's LAST kline is the
    current, still-in-progress day, so its volume is partial and would
    systematically understate "today's volume" (and dilute any average it
    participates in). All metrics below therefore use CLOSED candles only:
    the last element is dropped before computing anything.

    Returns dict (or None if < 8 closed candles):
        vol_24h_usd:      quote volume of the last CLOSED daily candle
        vol_avg_7d_usd:   mean quote volume of the last 7 closed candles
        vol_ratio:        vol_24h / vol_avg_7d — spike detector.
                          ~1.0 = normal, >2.0 = volume spike (breakout /
                          capitulation / news), <0.5 = drying up
        vol_trend:        3d avg / 7d avg — same acceleration framing as
                          _compute_rvm's momentum, but for participation:
                          >1 = volume building, <1 = fading
    """
    closed = quote_volumes[:-1]  # drop in-progress candle
    if len(closed) < 8:
        return None
    vol_24h = closed[-1]
    avg_7d = sum(closed[-7:]) / 7
    avg_3d = sum(closed[-3:]) / 3
    if avg_7d <= 0:
        return None
    return {
        "vol_24h_usd": round(vol_24h, 0),
        "vol_avg_7d_usd": round(avg_7d, 0),
        "vol_ratio": round(vol_24h / avg_7d, 3),
        "vol_trend": round(avg_3d / avg_7d, 3),
    }


# Single place to swap endpoint. From networks where api.binance.com is
# blocked (e.g. many Indonesian ISPs), set BINANCE_BASE to the public
# market-data mirror: https://data-api.binance.vision
BINANCE_BASE = os.environ.get("BINANCE_BASE", "https://api.binance.com")

# Quote assets / patterns that are NOT altcoins in any useful sense:
# stablecoin-vs-USDT pairs and leveraged tokens would pollute a
# "top altcoins by volume" list with pairs whose price never trends.
_STABLE_BASES = {
    "USDC", "FDUSD", "TUSD", "DAI", "BUSD", "USDP", "EUR", "EURI",
    "AEUR", "USDE", "USD1", "XUSD", "PAXG",  # PAXG = gold proxy, not a trending alt
}
_LEVERAGED_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")


def filter_alt_usdt_pairs(tickers):
    """
    Pure filter over Binance /ticker/24hr rows -> altcoin USDT symbols
    sorted by 24h quote volume (desc). Excludes BTCUSDT (it's the
    benchmark, not an alt), stablecoin pairs, and leveraged tokens.
    Kept side-effect-free so the offline self-test can exercise it.
    """
    out = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or sym == "BTCUSDT":
            continue
        if sym.endswith(_LEVERAGED_SUFFIXES):
            continue
        if sym[:-4] in _STABLE_BASES:
            continue
        try:
            qv = float(t.get("quoteVolume", 0))
        except (TypeError, ValueError):
            continue
        out.append((sym, qv))
    out.sort(key=lambda x: -x[1])
    return [s for s, _ in out]


def rank_from_tickers(symbols, tickers):
    """
    Pure helper: given a curated symbol universe and Binance /ticker/24hr
    rows, return the universe sorted by 24h quote volume (desc), DROPPING
    any symbol not present in the tickers. The drop is deliberate — it
    auto-heals the curated list against delistings and ticker renames
    (e.g. FTM→S, EOS→A) instead of producing dead "unavailable" cards.
    """
    vol = {}
    for t in tickers:
        try:
            vol[t.get("symbol", "")] = float(t.get("quoteVolume", 0))
        except (TypeError, ValueError):
            continue
    present = [s for s in symbols if s in vol]
    present.sort(key=lambda s: -vol[s])
    return present


def rank_symbols_by_volume(symbols):
    """
    Live wrapper around rank_from_tickers. Returns None on failure
    (caller keeps the curated order as fallback).
    """
    try:
        r = requests.get(f"{BINANCE_BASE}/api/v3/ticker/24hr", timeout=15)
        r.raise_for_status()
        return rank_from_tickers(symbols, r.json())
    except (requests.RequestException, ValueError) as e:
        print(f"[analyzer] volume ranking failed: {e}", file=sys.stderr)
        return None


def discover_top_symbols(n=20):
    """
    Fetch Binance 24h tickers and return the top-n altcoin USDT pairs by
    quote volume. Returns None on failure (caller decides the fallback —
    same contract as fetch_klines).
    """
    try:
        r = requests.get(f"{BINANCE_BASE}/api/v3/ticker/24hr", timeout=15)
        r.raise_for_status()
        return filter_alt_usdt_pairs(r.json())[:n]
    except (requests.RequestException, ValueError) as e:
        print(f"[analyzer] top-symbol discovery failed: {e}", file=sys.stderr)
        return None


def fetch_klines(symbol, interval="1d", limit=100):
    """
    Fetch daily klines for any Binance-listed symbol. No API key required
    (public endpoint) — this is what makes multi-coin support cheap: the
    exact same function serves ETHUSDT, SOLUSDT, or any other pair.

    Returns list of (timestamp_ms, high, low, close_price, quote_volume) oldest-first,
    or None on failure (network error, invalid/unlisted symbol, rate limit).

    quote_volume is kline index 7 (volume in the QUOTE asset, i.e. USDT for
    *USDT pairs) rather than index 5 (base-asset volume) — quote volume is
    directly comparable across coins ("$X traded"), base volume is not
    (1M SOL != 1M PEPE in any meaningful sense).
    """
    try:
        r = requests.get(
            f"{BINANCE_BASE}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        klines = r.json()
        return [(int(k[0]), float(k[2]), float(k[3]), float(k[4]), float(k[7])) for k in klines]
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        print(f"[Analyzer] {symbol} kline fetch failed: {e}", file=sys.stderr)
        return None


def analyze_coin(symbol, btc_closes=None, klines=None):
    """
    Compute a coin's technical state: RSI, return/volatility/momentum,
    and (if btc_closes is provided) its ratio trend vs BTC.

    Args:
        symbol: Binance pair, e.g. "ETHUSDT", "SOLUSDT"
        btc_closes: optional pre-fetched list of BTC closes (same interval
            and length as this coin's), to compute the vs-BTC ratio
            without an extra API call if the caller already has it
            (collect.py fetches BTC once and passes it to every coin).

    Returns:
        dict with keys: symbol, rsi, return/volatility/momentum (from
        _compute_rvm), and — if btc_closes given — btc_ratio_trend (the
        rate of change of coin/BTC ratio over the available window,
        positive = coin outperforming BTC = "altseason" direction for
        this specific coin).
        Returns {"symbol": symbol, "status": "unavailable"} if the kline
        fetch fails — callers should check for the "status" key before
        assuming numeric fields are present.
    """
    klines = klines if klines is not None else fetch_klines(symbol)
    if klines is None:
        return {"symbol": symbol, "status": "unavailable"}

    highs = [h for _, h, _, _, _ in klines]
    lows = [l for _, _, l, _, _ in klines]
    closes = [c for _, _, _, c, _ in klines]
    quote_volumes = [v for _, _, _, _, v in klines]
    rvm = _compute_rvm(closes)
    rsi = _compute_rsi(closes)
    vol = _compute_volume_metrics(quote_volumes)
    from altcoin.features import compute_feature_set
    feats = compute_feature_set(highs, lows, closes, quote_volumes[:-1], btc_closes)

    result = {
        "symbol": symbol,
        "status": "ok",
        "latest_price": closes[-1],
        "rsi": rsi,
        # 30d series for frontend sparklines. Volumes are CLOSED candles only
        # (last in-progress candle dropped) so the last bar isn't a false dip.
        "closes_30d": [round(c, 6) for c in closes[-30:]],
        "features": feats,
        "volumes_30d": [round(v, 0) for v in quote_volumes[:-1]][-30:],
        **(rvm or {}),
        **(vol or {}),
    }

    if btc_closes and min(len(btc_closes), len(closes)) >= 8:
        n = min(len(btc_closes), len(closes))  # tail-align: both end at latest candle
        ratios = [c / b for c, b in zip(closes[-n:], btc_closes[-n:]) if b]
        ratio_rvm = _compute_rvm(ratios)
        result["btc_ratio_trend"] = ratio_rvm["momentum"] if ratio_rvm else None
        result["btc_ratio_latest"] = round(ratios[-1], 6) if ratios else None
    else:
        result["btc_ratio_trend"] = None
        result["btc_ratio_latest"] = None

    return result


def analyze_multiple_coins(symbols, btc_symbol="BTCUSDT"):
    """
    Analyze multiple coins in one call, fetching BTC klines exactly once
    and reusing them for every coin's ratio calculation — this is the
    concrete mechanism that keeps multi-coin tracking cheap (1 + N API
    calls for N coins, not 2N).

    Args:
        symbols: list of Binance pairs, e.g. ["ETHUSDT", "SOLUSDT"]

    Returns:
        dict {symbol: analyze_coin() result}
    """
    btc_klines = fetch_klines(btc_symbol)
    btc_closes = [c for _, _, _, c, _ in btc_klines] if btc_klines else None
    if btc_closes is None:
        print(f"[Analyzer] WARNING: {btc_symbol} fetch failed — "
              f"btc_ratio_trend will be unavailable for all coins this cycle",
              file=sys.stderr)

    results = {}
    for symbol in symbols:
        results[symbol] = analyze_coin(symbol, btc_closes=btc_closes)
        if results[symbol].get("status") == "ok":
            results[symbol]["data_source"] = "binance"
        time.sleep(0.2)  # light rate-limit courtesy, Binance public API has generous but non-zero limits

    # ── Tier 2: Bybit (keyless, full OHLCV parity) ──
    # Same tuple shape as Binance klines, so recovered coins go through
    # the identical analyze_coin pipeline: volume indicators, features,
    # everything — only the data_source tag differs.
    failed = [s for s, r in results.items() if r.get("status") != "ok"]
    if failed:
        from altcoin.bybit_fallback import fetch_klines_bybit
        print(f"[Analyzer] Binance failed for {failed} — trying Bybit", file=sys.stderr)
        bb_btc_closes = btc_closes
        if bb_btc_closes is None:
            bb_btc = fetch_klines_bybit(btc_symbol)
            bb_btc_closes = [c for _, _, _, c, _ in bb_btc] if bb_btc else None
        for symbol in failed:
            kl = fetch_klines_bybit(symbol)
            if kl:
                res = analyze_coin(symbol, btc_closes=bb_btc_closes, klines=kl)
                if res.get("status") == "ok":
                    res["data_source"] = "bybit"
                    results[symbol] = res
            time.sleep(0.15)

    # ── Tier 3: CoinStats (price-only, credit-metered — last resort) ──
    failed = [s for s, r in results.items() if r.get("status") != "ok"]
    if failed:
        from altcoin.coinstats_fallback import analyze_coins_fallback, is_configured
        if is_configured():
            print(f"[Analyzer] Bybit couldn't recover {failed} — trying CoinStats",
                  file=sys.stderr)
            recovered = analyze_coins_fallback(failed, _compute_rvm, _compute_rsi)
            for symbol, res in recovered.items():
                results[symbol] = res
        still = [s for s in failed if results[s].get("status") != "ok"]
        if still:
            print(f"[Analyzer] Unrecoverable this cycle: {still}", file=sys.stderr)
    return results


if __name__ == "__main__":
    # Self-test with synthetic data (no network) to verify the
    # multi-coin-ness of this design: two DIFFERENT synthetic "coins"
    # analyzed through the same code path, with distinguishable results,
    # proving nothing is hardcoded to one specific coin.
    print("=== Self-test: altcoin/analyzer.py (offline, synthetic data) ===\n")

    import numpy as np
    rng = np.random.default_rng(42)

    # Simulate two different coins with genuinely different price behavior.
    # NOTE: two earlier attempts at this synthetic data revealed an
    # important distinction worth keeping in the comments — this module's
    # "momentum" (short-window avg return minus long-window avg return)
    # specifically measures ACCELERATION, not the level of a trend, and
    # requires the breakout to be SHORTER than the long window (7 days)
    # so the long window straddles both the flat period and the breakout
    # (pulling its average down) while the short window (3 days) sits
    # entirely within the breakout (pulling its average up). A constant
    # compounding rate lasting longer than 7 days reads as momentum≈0
    # even while genuinely rising — confirmed by hand-calculation before
    # writing this — because both windows then average the SAME constant
    # rate. This is mathematically correct for what "momentum" measures
    # here (acceleration, not trend level), not a defect in _compute_rvm.
    btc_prices = list(np.cumsum(rng.normal(0, 100, 30)) + 60000)
    eth_ratio_series = [0.05] * 27 + [0.05 * (1.02 ** i) for i in range(1, 4)]  # flat 27 days, breakout in final 3
    eth_prices_outperforming = [btc_prices[i] * eth_ratio_series[i] for i in range(30)]
    sol_prices_flat_ratio = list(btc_prices[i] * 0.003 for i in range(30))  # SOL tracking BTC exactly (flat ratio, no breakout)

    rsi_eth = _compute_rsi(eth_prices_outperforming)
    rsi_sol = _compute_rsi(sol_prices_flat_ratio)
    print(f"ETH-like series RSI: {rsi_eth}")
    print(f"SOL-like series RSI: {rsi_sol}")
    assert rsi_eth is not None and rsi_sol is not None
    print("✅ PASS: RSI computed independently for two different synthetic coins\n")

    eth_ratio = [e / b for e, b in zip(eth_prices_outperforming, btc_prices)]
    sol_ratio = [s / b for s, b in zip(sol_prices_flat_ratio, btc_prices)]
    eth_ratio_rvm = _compute_rvm(eth_ratio)
    sol_ratio_rvm = _compute_rvm(sol_ratio)
    print(f"ETH-like vs BTC ratio momentum: {eth_ratio_rvm['momentum']:+.6f} (should be positive — designed to outperform)")
    print(f"SOL-like vs BTC ratio momentum: {sol_ratio_rvm['momentum']:+.6f} (should be near zero — designed to track BTC flatly)")
    assert eth_ratio_rvm["momentum"] > 0
    assert abs(sol_ratio_rvm["momentum"]) < abs(eth_ratio_rvm["momentum"])
    print("✅ PASS: btc_ratio_trend correctly distinguishes an outperforming coin from a flat-tracking one\n")

    # Volume metrics: 30 synthetic daily quote volumes. Construct a spike
    # on the last CLOSED candle (index -2) and a tiny in-progress candle at
    # the very end (index -1) that MUST be excluded — if the incomplete
    # candle leaked into the math, vol_ratio would collapse below 1.
    flat_vols = [100e6] * 28 + [300e6] + [5e6]  # 28 normal days, 1 spike day (closed), partial today
    vm = _compute_volume_metrics(flat_vols)
    print(f"Volume metrics on synthetic spike series: {vm}")
    assert vm is not None
    assert vm["vol_24h_usd"] == 300e6, "must use last CLOSED candle, not partial in-progress one"
    assert vm["vol_ratio"] > 2.0, "spike day vs 7d avg should read as a >2x volume spike"
    fading_vols = [100e6] * 23 + [100e6, 80e6, 60e6, 40e6, 30e6, 20e6] + [1e6]
    vm_fade = _compute_volume_metrics(fading_vols)
    print(f"Volume metrics on synthetic fading series: {vm_fade}")
    assert vm_fade["vol_trend"] < 1.0, "declining participation should read vol_trend < 1"
    assert vm_fade["vol_ratio"] < 0.6, "last closed day well below 7d avg"
    print("✅ PASS: volume metrics — spike detection, fade detection, incomplete-candle exclusion\n")

    # Top-N discovery filter: pure-function test with synthetic ticker rows.
    fake_tickers = [
        {"symbol": "ETHUSDT", "quoteVolume": "5000000000"},
        {"symbol": "BTCUSDT", "quoteVolume": "9999999999"},   # benchmark — must be excluded
        {"symbol": "USDCUSDT", "quoteVolume": "8000000000"},  # stable pair — excluded
        {"symbol": "FDUSDUSDT", "quoteVolume": "7000000000"}, # stable pair — excluded
        {"symbol": "ETHUPUSDT", "quoteVolume": "6000000000"}, # leveraged — excluded
        {"symbol": "SOLUSDT", "quoteVolume": "2000000000"},
        {"symbol": "DOGEUSDT", "quoteVolume": "3000000000"},
        {"symbol": "ETHBTC", "quoteVolume": "4000000000"},    # non-USDT — excluded
        {"symbol": "PEPEUSDT", "quoteVolume": "not_a_number"},# bad row — skipped, not crash
    ]
    ranked = filter_alt_usdt_pairs(fake_tickers)
    print(f"Filtered/ranked pairs: {ranked}")
    assert ranked == ["ETHUSDT", "DOGEUSDT", "SOLUSDT"], ranked
    print("✅ PASS: top-N filter — excludes BTC/stables/leveraged/non-USDT, ranks by volume, survives bad rows\n")

    # Volume ranking of a curated universe: unknown/delisted symbols must be
    # DROPPED (self-healing against renames like FTM->S), rest sorted by volume.
    curated = ["ETHUSDT", "FTMUSDT", "SOLUSDT", "TONUSDT"]
    fake = [{"symbol":"SOLUSDT","quoteVolume":"3e9"},{"symbol":"ETHUSDT","quoteVolume":"5e9"},
            {"symbol":"TONUSDT","quoteVolume":"1e8"},{"symbol":"BTCUSDT","quoteVolume":"9e9"}]
    ranked = rank_from_tickers(curated, fake)
    print(f"Ranked curated universe: {ranked}")
    assert ranked == ["ETHUSDT", "SOLUSDT", "TONUSDT"], ranked
    print("✅ PASS: curated-universe ranking — delisted symbol pruned, volume-desc order\n")

    # CoinStats fallback pure transforms
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from altcoin.coinstats_fallback import build_symbol_map, chart_to_daily
    from datetime import datetime, timezone, timedelta

    # symbol map: rank-order collision handling (first = biggest by mcap wins)
    smap = build_symbol_map([
        {"id": "ethereum", "symbol": "ETH", "volume": "5e9"},
        {"id": "fake-eth-clone", "symbol": "ETH", "volume": "100"},
        {"id": "solana", "symbol": "SOL", "volume": "2e9"},
        {"id": "broken-row", "symbol": None},
    ])
    assert smap["ETH"]["id"] == "ethereum" and "SOL" in smap and len(smap) == 2
    print("✅ PASS: coinstats symbol map — collision keeps highest-cap coin, bad rows skipped")

    # chart downsampling: 35 days of HOURLY points -> daily closes,
    # last-point-of-day wins, incomplete today dropped
    now = datetime.now(timezone.utc)
    rows = []
    for d in range(35, -1, -1):  # includes today
        day = now - timedelta(days=d)
        for h in range(0, 24, 6):
            ts = int(day.replace(hour=h, minute=0, second=0, microsecond=0).timestamp())
            rows.append([ts, 100 + d + h * 0.01, (100 + d + h * 0.01) / 60000, 0])
    usd, btc = chart_to_daily(rows, days=30)
    print(f"chart_to_daily: {len(usd)} daily closes, last={usd[-1]}")
    assert len(usd) == 30 and len(btc) == 30
    assert abs(usd[-1] - (100 + 1 + 18 * 0.01)) < 1e-9, "must be LAST point of yesterday, today excluded"
    assert all(abs(u / b - 60000) < 1e-6 for u, b in zip(usd, btc)), "btc ratio series preserved"
    print("✅ PASS: coinstats chart downsampling — hourly->daily, day-close semantics, today dropped\n")

    # Blueprint feature layer (features.py) — pure math checks
    from altcoin.features import (compute_feature_set, score_components,
                                  rsi_zone_score, pct_rank, rs_vs_btc)
    assert rsi_zone_score(60) == 1.0 and rsi_zone_score(43) == 0.0 and 0 < rsi_zone_score(50) < 1
    assert pct_rank(5, [1,2,3,4]) == 100.0 and pct_rank(0, [1,2,3,4]) == 0.0

    # synthetic uptrending coin vs flat BTC over 100 candles
    up = [100*(1.01**i) for i in range(100)]
    flat = [60000.0]*100
    highs = [c*1.02 for c in up]; lowsv = [c*0.98 for c in up]
    vols = [1e9]*99 + [2.5e9]
    f = compute_feature_set(highs, lowsv, up, vols, flat)
    assert f["rs_7d"] is not None and f["rs_7d"] > 0, "uptrend vs flat BTC must have positive RS"
    assert f["prox_30d_high"] == 1.0, "monotonic uptrend closes at its 30d high"
    assert f["green_ratio_30d"] == 1.0 and f["ema_slope"] > 0
    assert f["vol_pct_90d"] == 100.0, "volume spike day ranks p100"
    score, drivers = score_components(f, rsi=62, macro_component=60)
    print(f"feature score={score}, top driver={drivers[0]['component']} ({drivers[0]['contribution']:+})")
    assert score is not None and score > 70, "textbook breakout setup must score high"
    assert drivers and abs(sum(d['contribution'] for d in drivers) - (score-50)) < 0.5, \
        "driver contributions must decompose the score (explainability contract)"
    # graceful degradation: no features at all -> None (caller falls to v1)
    s_none, d_none = score_components({}, rsi=None, macro_component=None)
    assert s_none is None and d_none == []
    print("✅ PASS: feature engineering + composite score v2 with additive drivers\n")

    # Regime proxy (regime.py) — rule checks incl. severity ordering
    from altcoin.regime import classify_regime
    import random as _r; _r.seed(3)
    base = [60000.0]
    for _ in range(99): base.append(base[-1]*(1+_r.gauss(0.0005,0.005)))
    calm_up = [b*(1.004**i/1.0) for i,b in enumerate(base)]  # gentle drift, low vol
    r1 = classify_regime(calm_up, glf_score=65, repo_stress=0.2)
    assert r1["state"] == "BULL_TREND", r1
    crash = base[:70] + [base[69]*(0.96**i)*(1+_r.gauss(0,0.01+0.004*i)) for i in range(1,31)]
    r2 = classify_regime(crash, glf_score=40, repo_stress=0.3)
    assert r2["state"] == "CAPITULATION_RECOVERY", r2
    r3 = classify_regime(calm_up, glf_score=65, repo_stress=0.9)
    assert r3["state"] == "RISK_OFF", "repo stress must override bull (severity ordering)"
    assert classify_regime([1.0]*10)["state"] == "UNKNOWN"
    print(f"regimes: {r1['state']}, {r2['state']}, {r3['state']}")
    print("✅ PASS: deterministic regime proxy — bull/capitulation/risk-off + severity order\n")

    # Point-in-time history (history.py) — persistence in tmp db
    from altcoin.history import append_cycle, stats
    import tempfile, os as _os
    tmp = _os.path.join(tempfile.mkdtemp(), "h.db")
    n = append_cycle({"ETHUSDT":{"status":"ok","trend_score":70},
                      "DEADUSDT":{"status":"unavailable"}},
                     {"glf_score":60}, {"mode":"l1"}, {"state":"SIDEWAYS"},
                     path=tmp, today="2026-07-12")
    assert n == 1, "unavailable rows must not be stored"
    n2 = append_cycle({"ETHUSDT":{"status":"ok","trend_score":71}},
                      {"glf_score":61}, {"mode":"l1"}, {"state":"SIDEWAYS"},
                      path=tmp, today="2026-07-12")
    s = stats(path=tmp)
    assert s == {"days":1, "rows":1}, f"same-day recollect must upsert, not duplicate: {s}"
    append_cycle({"ETHUSDT":{"status":"ok"}}, {}, {}, {}, path=tmp, today="2026-07-13")
    assert stats(path=tmp)["days"] == 2
    print("✅ PASS: history persistence — upsert per (date,symbol), unavailable rows excluded\n")

    # Fundamental Intelligence (fundamentals.py) — pure scoring checks
    from altcoin.fundamentals import fundamental_scores, blend_composite, WEIGHTS as FW
    strong = {"tvl_now": 12e9, "tvl_30d_ago": 10e9,          # +20% TVL
              "fees_7d": 9e6, "fees_30d": 30e6,              # weekly run-rate accelerating
              "revenue_30d": 12e6, "holders_revenue_30d": 10.8e6}  # 90% value accrual
    s1, d1 = fundamental_scores(strong, mcap=2e9)
    print(f"strong protocol: F={s1}, VA={d1['value_accrual_ratio']}, P/F={d1['price_to_fees']}")
    assert s1 > 70 and d1["value_accrual_ratio"] == 0.9 and not d1["missing"] == list(FW)
    hollow = dict(strong, holders_revenue_30d=0.0)           # fees high, holders get 0
    s2, d2 = fundamental_scores(hollow, mcap=2e9)
    assert s2 < s1 - 15, "zero value-accrual must materially drag the score (blueprint's core thesis)"
    partial = {"tvl_now": 5e9, "tvl_30d_ago": 5e9}           # only TVL known
    s3, d3 = fundamental_scores(partial)
    assert s3 is not None and set(d3["components"]) == {"tvl"} and "value_accrual" in d3["missing"], \
        "missing components renormalized, never zero-filled"
    assert fundamental_scores({})[0] is None
    assert blend_composite(80, 60) == 72.0 and blend_composite(None, 60) is None
    print("✅ PASS: fundamental scoring — value-accrual thesis, renormalization, composite blend\n")

    # VaF v1.0 engine (vaf.py)
    from altcoin.vaf import (evaluate_token, compute_vfr, build_pillar,
                             apply_caps, PILLARS)
    strong_raw = {"tvl_now": 12e9, "tvl_30d_ago": 10e9, "fees_7d": 9e6,
                  "fees_30d": 30e6, "revenue_30d": 12e6,
                  "holders_revenue_30d": 10.8e6, "holders_revenue_7d": 2.6e6}
    weak_raw = {"tvl_now": 1e9, "tvl_30d_ago": 1.1e9, "fees_7d": 1e6,
                "fees_30d": 8e6, "revenue_30d": 3e6,
                "holders_revenue_30d": 0.0, "holders_revenue_7d": 0.0}
    peers = [strong_raw, weak_raw,
             {"tvl_now": 4e9, "fees_30d": 15e6}]
    feats = {"ir_7d": 1.4, "prox_30d_high": 0.7}
    drivers = [{"component":"breakout","value":78},{"component":"trend_consistency","value":70}]

    ov = {"AAVEUSDT": {"pq": {"moat": 10, "product_market_fit": 8, "execution_risk": 5},
                        "nf": {"token_demand_link": 8, "supply_cleanliness": 4},
                        "rg": {"value_capture_expansion": 9, "catalyst_strength": 5,
                                "repricing_friction": 4},
                        "otf": {"catalyst_timing": 6, "supply_timing": 6},
                        "vfr": {"dilution_30d_usd": 4.2e6, "quality": "Proxy"}}}
    row = evaluate_token("AAVEUSDT", strong_raw, mcap=2e9, features=feats,
                          trend_drivers=drivers, peer_raws=peers,
                          peer_pf=[5.5, 30.0, 12.0], overrides=ov)
    print(f"VaF row: PQ={row['pq']} NF={row['nf']} RG={row['rg']} VaF={row['vaf']} "
          f"({row['tier']}) OTF={row['otf']} VFR={row['vfr']['display']} "
          f"conf={row['confidence']} verdict={row['verdict']}")
    assert row["actual"] == round(row["pq"] + row["nf"], 1)
    assert row["vaf"] == round(row["pq"] + row["nf"] + row["rg"], 1)
    assert all(row["coverage"][k] == 1.0 for k in ("pq","nf","otf")), row["coverage"]
    assert row["vfr"]["display"].startswith("+") and row["vfr"]["quality"] == "Proxy"
    prov = row["pillars"]["nf"]["metrics"]["live_value_capture"]["provenance"]
    assert prov == "auto" and row["pillars"]["pq"]["metrics"]["moat"]["provenance"] == "manual"

    # Guardrail 5 / NF cap: revenue that never reaches the token caps NF at 15
    row_w = evaluate_token("WEAKUSDT", weak_raw, mcap=None, features=feats,
                            trend_drivers=drivers, peer_raws=peers, peer_pf=[],
                            overrides={"WEAKUSDT": {"nf": {"token_demand_link": 11,
                                                            "supply_cleanliness": 6}}})
    assert row_w["nf"] <= 15.0 and any("NF capped 15" in c for c in row_w["caps_applied"]), \
        (row_w["nf"], row_w["caps_applied"])
    # coverage guardrail: auto-only run (no overrides) cannot crown Elite
    row_auto = evaluate_token("AUTOUSDT", strong_raw, mcap=2e9, features=feats,
                               trend_drivers=drivers, peer_raws=peers,
                               peer_pf=[5.5, 30.0, 12.0], overrides={})
    assert row_auto["coverage"]["rg"] < 0.7 and "coverage" in row_auto["verdict"].lower() \
        if row_auto["vaf"] and row_auto["vaf"] >= 95 else True
    # VFR display conventions
    assert compute_vfr({"holders_revenue_30d": 10e6}, {"dilution_30d_usd": 5e6})["display"] == "+2.0x"
    assert compute_vfr({"holders_revenue_30d": 5e6}, {"dilution_30d_usd": 10e6})["display"] == "-2.0x"
    assert compute_vfr({"holders_revenue_30d": 3e6}, {"dilution_30d_usd": 0})["display"].startswith("Positive")
    assert compute_vfr({"holders_revenue_30d": 10e6}, None)["display"] == "N/A"
    assert compute_vfr({"holders_revenue_30d": 0.0}, None)["display"] == "Weak / no capture"
    print("✅ PASS: VaF engine — locked formulas, provenance, NF cap, coverage guardrail, VFR conventions\n")

    # Bybit fallback tier (bybit_fallback.py)
    from altcoin.bybit_fallback import rows_to_klines
    # Bybit sends NEWEST-first strings: [ts, open, high, low, close, vol, turnover]
    bybit_rows = [
        ["1720800000000", "101", "105", "99", "104", "12000", "1250000"],   # newest
        ["1720713600000", "100", "103", "98", "101", "11000", "1120000"],
        ["bad", "x"],                                                        # malformed -> skipped
        ["1720627200000", "99", "102", "97", "100", "10000", "1000000"],    # oldest
    ]
    kl = rows_to_klines(bybit_rows)
    assert len(kl) == 3 and kl[0][0] < kl[1][0] < kl[2][0], "must be oldest-first"
    assert kl[-1] == (1720800000000, 105.0, 99.0, 104.0, 1250000.0), \
        "tuple shape must be (ts, high, low, close, QUOTE volume/turnover)"

    # Parity: identical synthetic data through the injected-klines path
    # must produce identical metrics to the primary path — the whole point
    # of tier 2 is zero metric degradation.
    synth = []
    ts0 = 1700000000000
    for i, c in enumerate(up):  # reuse the 100-candle uptrend series
        synth.append((ts0 + i*86400000, c*1.02, c*0.98, c, 2.5e9 if i == 98 else (5e6 if i == 99 else 1e9)))
    res_injected = analyze_coin("TESTUSDT", btc_closes=flat, klines=synth)
    assert res_injected["status"] == "ok"
    assert res_injected["rsi"] is not None and res_injected["vol_ratio"] is not None
    assert res_injected["features"]["prox_30d_high"] == 1.0
    assert res_injected["vol_ratio"] > 2.0, "volume metrics fully alive on injected klines"
    print("✅ PASS: Bybit tier — newest-first reversal, turnover-as-quote-volume, full metric parity\n")

    # Multi-group universe resolver (collect.py)
    import os as _o, sys as _s
    _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
    from collect import resolve_groups, SYMBOL_GROUPS
    ms, gm, lbl = resolve_groups("l1,defi")
    assert lbl == "l1+defi" and len(ms) == len(set(ms)), "merge must dedupe"
    assert gm["ETHUSDT"] == ["l1"] and gm["AAVEUSDT"] == ["defi"]
    try:
        resolve_groups("l1,typo"); raise AssertionError("must reject unknown group")
    except ValueError:
        pass
    print(f"✅ PASS: multi-group resolver — merge {len(ms)} symbols, tagging, unknown-group rejection\n")

    print("ALL SELF-TESTS PASSED — confirms the same code path produces coin-specific,")
    print("distinguishable results for different symbols, not hardcoded output.")

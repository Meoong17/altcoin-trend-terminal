"""
CoinStats fallback data source.

Used ONLY when the primary Binance fetch fails for a coin (or for all
coins, e.g. ISP block without the mirror configured). Never dual-fetched:
on the free tier (20k credits/month) the charts endpoint costs
3 credits x number of coinIds, so this layer must stay dormant in the
happy path and spend credits only on actual failures.

What this fallback CAN restore (price-derived, from /coins/charts):
    closes_30d, latest_price, rsi, return/volatility/momentum,
    btc_ratio_trend  (chart rows carry price-in-BTC directly, so the
                      ratio series comes free — no separate BTC fetch)

What it CANNOT restore (CoinStats charts carry no historical volume):
    volumes_30d, vol_avg_7d_usd, vol_ratio, vol_trend
    vol_24h_usd IS included (current 24h volume from the /coins listing).

Every fallback result is tagged "data_source": "coinstats" so the
dashboard can show the degraded-volume state instead of silently
rendering an emptier card — same anti-silent-failure principle as the
GLF degraded banner.

Auth: COINSTATS_API_KEY env var (.env locally, repo secret in Actions).
The key is never hardcoded here.
"""

import os
import sys
from datetime import datetime, timezone

import requests

COINSTATS_BASE = "https://openapiv1.coinstats.app"


def _api_key():
    return os.environ.get("COINSTATS_API_KEY", "").strip() or None


def is_configured():
    return _api_key() is not None


def _get(path, params=None):
    r = requests.get(
        f"{COINSTATS_BASE}{path}",
        params=params or {},
        headers={"X-API-KEY": _api_key(), "accept": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


# ── Pure transforms (offline-testable) ──

def build_symbol_map(coins_rows):
    """
    /coins rows -> {"ETH": {"id": "ethereum", "volume": float}, ...}
    First occurrence wins: the listing is ranked by market cap, so on
    ticker-symbol collisions (many small tokens reuse majors' symbols)
    the largest coin — the one a USDT-pair universe means — is kept.
    """
    out = {}
    for row in coins_rows:
        sym = (row.get("symbol") or "").upper()
        cid = row.get("id")
        if not sym or not cid or sym in out:
            continue
        try:
            vol = float(row.get("volume") or 0)
        except (TypeError, ValueError):
            vol = 0.0
        try:
            mcap = float(row.get("marketCap") or 0) or None
        except (TypeError, ValueError):
            mcap = None
        out[sym] = {"id": cid, "volume": vol, "mcap": mcap}
    return out


def chart_to_daily(rows, days=30):
    """
    Chart rows [[ts_sec, price_usd, price_btc, price_eth], ...] ->
    (usd_closes, btc_ratio_series), both daily and oldest-first.

    Granularity of period=1m is provider-defined (may be hourly), so
    rows are bucketed by UTC date and the LAST point of each day is the
    daily close — matching how Binance daily klines close. The current
    (incomplete) UTC day is dropped for the same reason the volume
    metrics drop the in-progress candle.
    """
    by_day = {}
    order = []
    for row in rows or []:
        try:
            ts, usd, btc = int(row[0]), float(row[1]), float(row[2])
        except (TypeError, ValueError, IndexError):
            continue
        day = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        if day not in by_day:
            order.append(day)
        by_day[day] = (usd, btc)
    today = datetime.now(timezone.utc).date()
    days_sorted = [d for d in sorted(order) if d != today][-days:]
    usd_closes = [by_day[d][0] for d in days_sorted]
    btc_ratios = [by_day[d][1] for d in days_sorted]
    return usd_closes, btc_ratios


# ── Live fetchers ──

_SYMBOL_MAP_CACHE = None


def _symbol_map():
    global _SYMBOL_MAP_CACHE
    if _SYMBOL_MAP_CACHE is None:
        rows = _get("/coins", {"limit": 500, "currency": "USD"})
        if isinstance(rows, dict):
            rows = rows.get("result") or rows.get("coins") or []
        _SYMBOL_MAP_CACHE = build_symbol_map(rows)
    return _SYMBOL_MAP_CACHE


def fetch_charts_batch(coin_ids, period="1m"):
    """
    One batched /coins/charts call for every failed coin -> {coinId: rows}.
    Batching doesn't reduce credits (they multiply per coinId) but cuts
    round trips and respects the 2 req/s free-tier limit.
    Rows for coins with an errorMessage are skipped (partial success).
    """
    data = _get("/coins/charts", {"coinIds": ",".join(coin_ids), "period": period})
    out = {}
    for entry in data if isinstance(data, list) else []:
        cid = entry.get("coinId")
        if cid and entry.get("chart") and not entry.get("errorMessage"):
            out[cid] = entry["chart"]
    return out


def analyze_coins_fallback(symbols, compute_rvm, compute_rsi):
    """
    Fallback analysis for the given Binance-style symbols (e.g. ETHUSDT).
    compute_rvm / compute_rsi are injected from analyzer.py so both data
    sources share the exact same math (no metric drift between sources).

    Returns {symbol: result_dict} for the symbols it could recover.
    Symbols it can't map or chart stay absent — caller keeps them as
    "unavailable".
    """
    if not is_configured():
        return {}
    try:
        smap = _symbol_map()
    except (requests.RequestException, ValueError) as e:
        print(f"[coinstats] symbol map fetch failed: {e}", file=sys.stderr)
        return {}

    wanted = {}
    for symbol in symbols:
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        if base in smap:
            wanted[smap[base]["id"]] = (symbol, smap[base]["volume"])

    if not wanted:
        return {}

    try:
        charts = fetch_charts_batch(list(wanted))
    except (requests.RequestException, ValueError) as e:
        print(f"[coinstats] charts fetch failed: {e}", file=sys.stderr)
        return {}

    results = {}
    for cid, (symbol, vol24) in wanted.items():
        closes, btc_ratios = chart_to_daily(charts.get(cid))
        if len(closes) < 8:
            continue
        rvm = compute_rvm(closes)
        ratio_rvm = compute_rvm(btc_ratios) if len(btc_ratios) >= 8 else None
        results[symbol] = {
            "symbol": symbol,
            "status": "ok",
            "data_source": "coinstats",
            "latest_price": closes[-1],
            "rsi": compute_rsi(closes),
            "closes_30d": [round(c, 6) for c in closes],
            # CoinStats charts carry no historical volume:
            "volumes_30d": None,
            "vol_24h_usd": round(vol24, 0) if vol24 else None,
            "vol_avg_7d_usd": None,
            "vol_ratio": None,
            "vol_trend": None,
            **(rvm or {}),
            "btc_ratio_trend": ratio_rvm["momentum"] if ratio_rvm else None,
        }
    return results


def mcaps_for(symbols):
    """{BinanceSymbol: market_cap} via the cached listing; {} if the
    CoinStats key isn't configured — VaF then reports the valuation
    metric as n/a instead of guessing."""
    if not is_configured():
        return {}
    try:
        smap = _symbol_map()
    except Exception:
        return {}
    out = {}
    for symbol in symbols:
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        if base in smap and smap[base].get("mcap"):
            out[symbol] = smap[base]["mcap"]
    return out

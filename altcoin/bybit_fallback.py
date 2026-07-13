"""
Bybit fallback — tier 2 of the data-source chain:

    Binance (primary) -> Bybit (this module) -> CoinStats (last resort)

Why Bybit sits above CoinStats in the chain:
    - No API key, no credit system — a public market-data endpoint like
      Binance's, so the tier costs nothing to keep warm.
    - Full OHLCV WITH quote volume ("turnover"), so a Bybit-served coin
      keeps 100% of the metric set: volume indicators, ATR/BB features,
      VaF OTF inputs — none of the degradation the CoinStats tier has.
    - Symbol strings are identical to Binance (ETHUSDT), so no id
      mapping layer at all.

Endpoint: GET {BYBIT_BASE}/v5/market/kline
    params: category=spot, symbol, interval=D, limit
    response.result.list rows (NEWEST FIRST — must be reversed):
        [startTime_ms, open, high, low, close, volume(base), turnover(quote)]

BYBIT_BASE env override mirrors BINANCE_BASE: Bybit is also blocked on
some Indonesian ISPs; GitHub Actions runners are unaffected.
"""

import os
import sys

import requests

BYBIT_BASE = os.environ.get("BYBIT_BASE", "https://api.bybit.com")


def rows_to_klines(rows):
    """
    Pure transform: Bybit result.list -> the exact tuple shape
    analyzer.fetch_klines returns: [(ts_ms, high, low, close, quote_vol)]
    OLDEST-first. Bybit sends newest-first strings; malformed rows are
    skipped rather than crashing the tier.
    """
    out = []
    for row in rows or []:
        try:
            out.append((int(row[0]), float(row[2]), float(row[3]),
                        float(row[4]), float(row[6])))
        except (TypeError, ValueError, IndexError):
            continue
    out.sort(key=lambda t: t[0])
    return out


def fetch_klines_bybit(symbol, interval="D", limit=100):
    """Same contract as analyzer.fetch_klines: klines or None on failure."""
    try:
        r = requests.get(
            f"{BYBIT_BASE}/v5/market/kline",
            params={"category": "spot", "symbol": symbol,
                    "interval": interval, "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("retCode") != 0:
            print(f"[bybit] {symbol}: retCode={data.get('retCode')} "
                  f"{data.get('retMsg')}", file=sys.stderr)
            return None
        klines = rows_to_klines((data.get("result") or {}).get("list"))
        return klines or None
    except (requests.RequestException, ValueError) as e:
        print(f"[bybit] {symbol}: {e}", file=sys.stderr)
        return None

"""
OKX fallback — tier 2 of the data-source chain (verified wired into analyzer.py):

    Binance (primary) -> OKX (this module) -> Bybit -> CoinStats (last resort)

Why OKX sits above Bybit (was tier 2) in the chain:
    - Consistent high liquidity across major altcoins, comparable to Binance.
    - Same symbol format (BTC-USDT vs Binance's BTCUSDT) with trivial mapping.
    - Public market-data endpoint needs no API key — zero friction to fall back.
    - Full OHLCV WITH quote volume ("volCcy"), so a OKX-served coin keeps 100%
      of the metric set: volume indicators, ATR/BB features, VaF OTF inputs.
    - API key provided via OKX_API_KEY env var is accepted but NOT required
      for market data; it may grant higher rate limits on paid tiers.

Endpoint: GET https://www.okx.com/api/v5/market/candles
    params: instId=BTC-USDT, bar=1Dutc, limit=100
    response.data rows (NEWEST FIRST — must be reversed):
        [ts_ms, o, h, l, c, vol(base), volCcy(quote), volCcyQuote, confirm]
"""

import os
import sys

import requests

OKX_BASE = os.environ.get("OKX_BASE", "https://www.okx.com")
OKX_API_KEY = os.environ.get("OKX_API_KEY", "").strip()


def _symbol_okx(binance_symbol):
    """Convert Binance-style BTCUSDT to OKX-style BTC-USDT."""
    if binance_symbol.endswith("USDT"):
        return binance_symbol[:-4] + "-USDT"
    if binance_symbol.endswith("USDC"):
        return binance_symbol[:-4] + "-USDC"
    return binance_symbol


def _headers():
    """Return headers for an OKX request.
    Public market-data endpoints work without auth, but if an API key is
    configured we include it for potential rate-limit benefits.
    """
    h = {"Accept": "application/json"}
    if OKX_API_KEY:
        h["OK-ACCESS-KEY"] = OKX_API_KEY
    return h


def rows_to_klines(rows):
    """
    Pure transform: OKX /candles response rows -> the exact tuple shape
    analyzer.fetch_klines returns: [(ts_ms, high, low, close, quote_vol)]
    OLDEST-first. OKX sends newest-first strings; malformed rows are
    skipped rather than crashing the tier.
    """
    out = []
    for row in rows or []:
        try:
            # row: [ts, o, h, l, c, vol(base), volCcy(quote), volCcyQuote, confirm]
            out.append((int(row[0]), float(row[2]), float(row[3]),
                        float(row[4]), float(row[6])))
        except (TypeError, ValueError, IndexError):
            continue
    out.sort(key=lambda t: t[0])
    return out


def fetch_klines_okx(symbol, interval="D", limit=100):
    """Same contract as analyzer.fetch_klines: klines or None on failure.

    Args:
        symbol: Binance-style symbol (e.g. BTCUSDT) — converted internally.
        interval: 'D' for daily, 'H4' for 4-hour, etc. (OKX bar format).
        limit: Max candles to fetch.
    """
    okx_symbol = _symbol_okx(symbol)
    # Map common intervals to OKX bar format
    bar_map = {
        "1d": "1Dutc",
        "D": "1Dutc",
        "4h": "4Hutc",
        "H4": "4Hutc",
        "1h": "1Hutc",
        "H1": "1Hutc",
    }
    bar = bar_map.get(interval, "1Dutc")

    try:
        r = requests.get(
            f"{OKX_BASE}/api/v5/market/candles",
            params={"instId": okx_symbol, "bar": bar, "limit": limit},
            headers=_headers(),
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != "0":
            print(f"[okx] {symbol}: code={data.get('code')} {data.get('msg')}",
                  file=sys.stderr)
            return None
        klines = rows_to_klines(data.get("data"))
        return klines or None
    except (requests.RequestException, ValueError) as e:
        print(f"[okx] {symbol}: {e}", file=sys.stderr)
        return None

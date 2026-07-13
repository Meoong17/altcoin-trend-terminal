# Altcoin Trend Terminal

Multi-coin trend analysis, sharing a global liquidity macro layer
(originally built for [sfc-terminal](../sfc-terminal), a BTC-focused
liquidity stress model) across any number of tracked altcoins.

## Why this is a separate repo

The macro liquidity layer (Fed/ECB/BOJ/China/M2/TGA/RRP/DXY, repo market
stress) is asset-agnostic — it measures global dollar liquidity
conditions, not anything BTC-specific — so it's genuinely reusable here.
But most of sfc-terminal's other 90+ signals (on-chain metrics, ETF
flows, price-outcome-trained ML models) are BTC-specific by construction
and don't transfer to arbitrary altcoins without being rebuilt per-coin.
Rather than bolt a partial altcoin system onto sfc-terminal, this is a
clean, minimal, standalone project — see `docs/ARCHITECTURE.md` for the
full reasoning.

## Structure

```
collect.py                          # entry point — run this
liquidity/
  global_liquidity_engine.py        # GLF — copied from sfc-terminal, unchanged, already tested
  repo_market_stress.py             # M86 SOFR-EFFR — copied from sfc-terminal, unchanged, already tested
altcoin/
  analyzer.py                       # NEW — generic, symbol-parameterized (any Binance pair)
```

## Multi-coin by design

`altcoin/analyzer.py` takes a `symbol` parameter (e.g. `"ETHUSDT"`,
`"SOLUSDT"`) — the same code serves any Binance-listed pair, no
per-coin code duplication. The macro layer (GLF, repo stress) is
computed once per cycle in `collect.py` and shared across every tracked
coin, not recomputed per coin.

To track different coins, edit `TRACKED_SYMBOLS` in `collect.py`, or run:

```bash
python3 collect.py --symbols ETHUSDT,SOLUSDT,BNBUSDT
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# fill in FRED_API_KEY (needed for the macro layer only — Binance data needs no key)
python3 collect.py
```

## ⚠️ Validation status — read before trusting trend_score

`collect.py`'s `compute_coin_trend_score()` is a **simple, unvalidated
heuristic** (weighted blend of RSI + BTC-relative momentum + macro
backdrop). Unlike sfc-terminal's BTC model — which went through
extensive circular-labeling fixes and was retrained on real
price-outcome data before its accuracy claims meant anything — this
altcoin scoring has had **no such validation yet**. Treat `trend_score`
as a directional signal to monitor and backtest, not a trustworthy
prediction, until it's been tested against real forward price outcomes
the way sfc-terminal's `ml_ensemble.py` was.

## Status

Initial scaffold. `liquidity/` modules carry over sfc-terminal's audit
history (see that repo's `docs/PROJECT_STATUS.md`) — both were tested
there before being copied here unchanged. `altcoin/analyzer.py` and
`collect.py` are new and have only been self-tested with synthetic data
(no live network access during development — see each file's
`if __name__ == "__main__"` block for what was verified offline).
Run against live data and validate before relying on any output.

# Architecture

## Why a separate repo, not a module inside sfc-terminal

sfc-terminal's ~93 signals split roughly into two categories:

**Asset-agnostic (macro)** — Fed/ECB/BOJ/China/M2/TGA/RRP/DXY (Global
Liquidity Factor), SOFR-EFFR spread (repo market stress). These measure
global dollar liquidity conditions, unrelated to which crypto asset
you're predicting. **Copied into this repo unchanged** — both modules
already went through bug-fixing and testing in sfc-terminal (see that
repo's `docs/PROJECT_STATUS.md` for the audit history) before being
copied here.

**BTC-specific** — on-chain metrics (MVRV, whale ratio, exchange flows —
all computed from BTC blockchain data specifically), ETF flows (BTC spot
ETFs exist; most altcoins don't have equivalents yet), and every ML
model's training target (price-outcome labels built from BTC's own price
history). None of this transfers to an arbitrary altcoin without being
rebuilt per-coin from different data sources.

Rather than partially extend sfc-terminal with altcoin logic mixed into
a BTC-focused codebase, this is a clean separate project: reuse what's
genuinely reusable (the macro layer), build fresh what's coin-specific
(technical + relative-strength layer in `altcoin/analyzer.py`).

## Data flow

```
collect.py
    │
    ├─ liquidity/global_liquidity_engine.py  → GLF score      ┐
    ├─ liquidity/repo_market_stress.py       → repo stress    │  computed ONCE,
    │                                                          │  shared across
    │                                                          │  all coins
    └─ altcoin/analyzer.py (per symbol)                        │
         ├─ fetch_klines(symbol)   → price history             │
         ├─ _compute_rsi()         → RSI-14                    │
         ├─ _compute_rvm()         → return/vol/momentum       │
         └─ vs BTC ratio trend     → relative strength         │
                                                                 │
    compute_coin_trend_score(coin_result, GLF, repo_stress) ←──┘
         → trend_score (0-100) per coin
```

## Extending to a new coin

Add the Binance pair to `TRACKED_SYMBOLS` in `collect.py`, or pass
`--symbols`. No other code changes — `altcoin/analyzer.py` is fully
generic (see its self-test, which verifies two *different* synthetic
coins produce distinguishable results through the same code path).

## Known limitations

- `compute_coin_trend_score()` weights (0.35 RSI / 0.40 relative
  strength / 0.25 macro) are unvalidated starting values, not derived
  from backtesting. See README's validation status section.
- No ETF, on-chain, or ML-model layer yet — this is a technical +
  macro-only MVP.
- `altcoin/analyzer.py` duplicates `_compute_rvm()` from sfc-terminal's
  `market_data_fetcher.py` rather than importing across repos, since
  this is intentionally a standalone project (see top of this doc).

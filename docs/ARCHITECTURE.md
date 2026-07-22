# Architecture

## Why a separate repo, not a module inside sfc-terminal

sfc-terminal's signals split roughly into two categories:

**Asset-agnostic (macro)** — Fed/ECB/BoJ/China M2/US M2/TGA/RRP/DXY
(Global Liquidity Factor), SOFR-EFFR spread (repo market stress),
stablecoin supply growth. These measure global dollar liquidity
conditions, unrelated to which crypto asset you're evaluating —
genuinely reusable, copied here largely unchanged.

**BTC-specific** — on-chain metrics, ETF flows, and price-outcome-
trained ML models are all built from BTC's own data and don't transfer
to arbitrary altcoins without being rebuilt per-coin from different
sources. This repo builds that layer fresh, generically, per-symbol.

## Data flow

```
collect.py
  │
  ├─ liquidity/global_liquidity_engine.py    → GLF score           ┐
  ├─ liquidity/repo_market_stress.py         → repo stress          │ computed ONCE,
  ├─ liquidity/stablecoin_liquidity.py       → stablecoin score     │ shared across
  ├─ BTC dominance (CoinGecko /global)       → market structure     │ all coins
  │                                                                  │
  ├─ altcoin/analyzer.py (per symbol, multi-tier fallback)          │
  │    Binance (primary)                                            │
  │      └─ fails → altcoin/bybit_fallback.py (keyless, full parity)│
  │           └─ fails → altcoin/coinstats_fallback.py (price-only) │
  │    each tier returns: RSI, RVM, volume metrics, features,       │
  │    compute_performance() vs BTC/ETH — same functions regardless │
  │    of which tier served the coin                                │
  │                                                                  │
  ├─ altcoin/features.py     → Trend Score v2 + additive drivers    │
  ├─ altcoin/regime.py       → regime state + hysteresis            │
  ├─ altcoin/fundamentals.py → F-score (DeFi/infra coins only)       │
  ├─ altcoin/vaf.py          → VaF v1.0 (DeFi/infra coins only)      │
  ├─ altcoin/news.py         → sentiment/catalysts (display-only)    │
  ├─ altcoin/correlation.py  → portfolio concentration warning       │
  │                                                                  │
  ├─ altcoin/history.py      → point-in-time SQLite persistence     │
  └─ altcoin/alerts.py        → Telegram alert on exception/staleness┘
       │
       ▼
   data.json  →  index.html (dashboard)
```

## Universe & sector taxonomy

`collect.py`'s `SYMBOL_GROUPS` is a hardcoded curation, not an API
category lookup — third-party category APIs drift and misclassify.
Each symbol belongs to exactly one sector; conflicts resolve to the
more fundamental identity (e.g. LINK/PYTH are `infra`, not `defi`,
even though their fee data lives in the same DeFiLlama-backed map used
for VaF/F-score — `SECTOR_LOOKUP` is intentionally kept separate from
`DEFI_PROTOCOLS` so the two concerns can't collide).

Coins discovered via `--top`/`--extend-top` (not from a curated group)
get tagged via `SECTOR_LOOKUP` if they're a known symbol, or `"other"`
otherwise — every tracked coin has a sector, regardless of how it was
selected.

## Data-source fallback chain

Four tiers, in order, each degrading gracefully rather than failing
silently:

1. **Binance** (primary) — full OHLCV, all metrics.
2. **OKX** (keyless fallback) — high liquidity comparable to Binance,
   same tuple shape as Binance klines, fed through the *identical*
   analysis pipeline. Zero metric degradation; only `data_source` differs.
3. **Bybit** (keyless fallback) — same contract as OKX, tried next.
4. **CoinStats** (last resort, credit-metered) — price-only. Volume
   history is unavailable and explicitly flagged in the UI
   (`CS FALLBACK` badge); `compute_performance()` reconstructs implied
   BTC/ETH USD series from price ratios so vs-BTC/vs-ETH figures use
   the *same formula* as the other tiers rather than a second,
   subtly different one under the same field name.

## Honesty conventions (apply everywhere)

- Missing data → `None`, weight excluded from whatever composite it
  feeds, never silently zero-filled.
- Every renormalized score reports how many of its possible components
  were actually used (`coverage`), so two equal-looking scores aren't
  assumed comparable if their component sets differ.
- VaF's Confidence grade (A–D) is derived from data coverage; manual
  overrides can only lower it, never raise it.
- News sentiment and portfolio-correlation warnings are display-only —
  neither ever feeds a score automatically.

## Validation status

Every composite weight in this system is a documented heuristic, not a
backtested one. `history.py`'s schema includes `backtest_runs` and
`decision_quality_history` tables specifically so that once
`history.db` has enough accumulated point-in-time history, these
weights can be tested against real forward returns — that validation
has not happened yet. Treat every score as a research-prioritization
signal, not a trading signal, until it does.

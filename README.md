# Altcoin Trend Terminal

Macro-aware altcoin trend, volume, fundamental, and value-accrual
intelligence — a public research dashboard covering L1s, DeFi, oracle
infrastructure, AI/compute, and more, sharing a global liquidity macro
layer originally built for [sfc-terminal](../sfc-terminal) (a
BTC-focused liquidity stress model).

Research tooling — not trading advice; every score is a documented
heuristic until it's been validated against real forward returns (see
**Epistemic status** below).

## What it does

- **Trend Score v2** — per-coin technical composite (breakout, relative
  strength vs BTC, trend consistency, volatility compression, macro
  backdrop), with an additive driver breakdown and a reported coverage
  count so two coins' scores are only compared when built from the same
  components.
- **Performance panel** — precise 1D/7D/30D returns and excess return
  vs BTC and vs ETH, computed identically across every data-source tier.
- **Macro layer** — Global Liquidity Factor (Fed/ECB/BoJ/China M2/US
  M2/TGA/RRP/DXY), repo market stress (SOFR−EFFR), stablecoin supply
  growth, BTC dominance trend.
- **Regime detection** — deterministic 5-state classifier (bull/bear/
  sideways/risk-off/capitulation) with 6-day hysteresis so it doesn't
  flip on noise; severity escalation is never delayed.
- **Altcoin Season Index** — breadth of coins beating BTC over 90 days.
- **Fundamental F-Score & VaF v1.0** — for DeFi/infra coins: TVL growth,
  revenue yield, fee acceleration, and value-accrual share from
  DeFiLlama, plus the full locked Direct Value Accrual Framework
  (PQ/NF/RG/OTF/VFR) with per-metric provenance (auto/proxy/manual) and
  a coverage guardrail that prevents an auto-only run from crowning an
  "Elite" verdict.
- **News sentiment & catalyst flags** — CryptoPanic community votes,
  display-only, never feeds any score automatically.
- **Portfolio concentration warning** — flags when the top-scoring
  coins are highly correlated (not real diversification).
- **Sector taxonomy** — l1, l2, defi, infra, ai, meme, gaming, depin,
  rwa, privacy, restaking; every tracked coin gets exactly one sector,
  including coins discovered via `TOP_N`/`EXTEND_TOP` (not just the
  curated groups).
- **Three-tier data fallback** — Binance (primary) → Bybit (keyless
  fallback, full metric parity) → CoinStats (last resort, price-only,
  credit-metered). A coin never goes silently unavailable if any tier
  can serve it.
- **Operational alerting** — Telegram alerts on collector exceptions
  and stale data, plus an independent cron-based `watchdog.sh` that
  checks `data.json`'s age and structural health hourly.

## Structure

```
collect.py                              # entry point — run this
altcoin/
  analyzer.py                           # per-coin analysis, multi-tier fetch orchestration
  features.py                           # trend score v2 composite + additive drivers
  regime.py                             # regime classifier + hysteresis
  fundamentals.py                       # DeFiLlama fundamental F-score
  vaf.py                                # VaF v1.0 (locked framework)
  history.py                            # point-in-time SQLite persistence
  correlation.py                        # portfolio concentration warning
  news.py                               # CryptoPanic sentiment & catalysts
  alerts.py                             # Telegram alerting (exceptions, staleness)
  bybit_fallback.py / coinstats_fallback.py
liquidity/
  global_liquidity_engine.py            # GLF — from sfc-terminal, unchanged
  repo_market_stress.py                 # SOFR-EFFR — from sfc-terminal, unchanged
  stablecoin_liquidity.py               # DeFiLlama stablecoin supply growth
index.html                              # dashboard (table + card views)
vaf_overrides.json                      # manual VaF research inputs (see below)
watchdog.sh                             # hourly cron health check + Telegram alert
```

## Universe selection

Pick ONE mode via `.env` or CLI flags:

```bash
# Curated sector group(s) — comma-separated, combinable
python3 collect.py --group l1              # 31 Layer-1 coins
python3 collect.py --group l1,defi,infra,ai
python3 collect.py --group all             # every curated sector

# Curated group + market-wide breadth beyond it
python3 collect.py --group l1 --extend-top 400

# Free-form
python3 collect.py --symbols ETHUSDT,SOLUSDT,BNBUSDT
python3 collect.py --top 20                # top-20 by 24h volume, no curation
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# required: FRED_API_KEY (macro layer)
# optional: COINSTATS_API_KEY, CRYPTOPANIC_API_KEY,
#           TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
python3 collect.py --group l1,defi,infra,ai
python3 -m http.server 8081   # or your existing nginx config
```

Full VPS deployment steps (nginx, cron, backup-to-GitHub) are covered
in the deployment runbook — ask if you need it regenerated for this repo.

## VaF manual research (`vaf_overrides.json`)

VaF v1.0's judgment metrics (Moat, Product-Market Fit, Catalyst
Strength, etc.) are deliberately NOT computed by a formula — they need
an analyst's read. Fill them in per protocol in `vaf_overrides.json`
(template: `vaf_overrides.example.json`). Coverage below 70% on a core
pillar caps the verdict at "coverage-limited" regardless of the
numeric score, so an auto-only run can nominate but never crown an
Elite candidate — filling this file in is what unlocks full verdicts.

**Never commit real Telegram/API tokens into this repo.** Secrets go
in `.env` (gitignored), never in `.py`/`.sh`/`.json` files that get
committed.

## Epistemic status — read before trusting any score

Every composite weight in this system (Trend Score's 30/30/20/10/10
split, GLF's component weights, VaF's metric weights, F-Score's
component weights) is a **documented heuristic, not yet validated
against real forward returns**. The system is built to be honest about
this at every layer: missing data is represented as `None` with
weights renormalized across what remains, never silently zero-filled;
VaF's confidence grade degrades automatically when coverage is thin;
news sentiment and correlation warnings are display-only and never
feed a score automatically.

`history.db` accumulates a point-in-time snapshot every collection
cycle specifically so that, once enough history exists, these weights
can be backtested against real outcomes rather than trusted on
priors. Until that validation happens, treat every score here as a
research-prioritization signal, not a trading signal.

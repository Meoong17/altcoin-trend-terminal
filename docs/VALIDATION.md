# Predictive Validation Notes

Empirical record of what has and hasn't been validated against real
forward returns. **Read before trusting any score — and before
re-testing anything already settled here.** The goal is honesty: record
what holds, what was falsified, and what is still untestable with the
data we have. Anything marked **REJECTED / NOT EDGE** should not be
re-built or re-tested without new data or a new mechanism, not just a
new hope.

---

## Cross-sectional momentum (altcoins) — REJECTED (cross-era)

**Verdict (17 Aug 2026):** cross-sectional momentum in altcoins does
**not** have a stable edge across market regimes. Reject as a predictive
signal. This is the final word, now tested on ~9 years of data across
era splits — not a single-era under-powered sample.

**Method**
- 14 coins, 2017–2026 (~9 years), daily forward returns.
- Split by regime: BULL / SIDEWAYS / BEAR, each at lookbacks 7 / 14 /
  30 / 60 days.
- Metric: mean cross-sectional Information Coefficient (Spearman IC of
  past return vs forward return) and fraction of days IC>0.

**Result (mean IC, IC>0 frac)**

| regime   | L=7   | L=14  | L=30  | L=60  |
|----------|-------|-------|-------|-------|
| BULL     | −0.041 (44%) | −0.008 (48%) | −0.046 (46%) | −0.055 (47%) |
| SIDEWAYS | −0.010 (48%) | +0.043 (55%) | +0.038 (56%) | +0.000 (50%) |
| BEAR     | −0.019 (48%) | −0.018 (45%) | −0.036 (47%) | −0.087 (42%) |

**Interpretation**
1. In BULL — the regime where classical momentum should help most — IC
   is **negative at every lookback** (−0.008 … −0.055). Momentum does
   not work even during a rising trend.
2. In SIDEWAYS, L14/L30 are weakly positive (+0.038–0.043, frac ~55%)
   but **not consistent**: L7 and L60 are ~0. This is noise, not edge.
3. In BEAR, consistently negative (momentum selects falling coins —
   expected, but not an exploitable edge).
4. IC>0 fraction sits at 44–56% everywhere — around a coin flip. No
   regime reaches the bar for a real edge (IC>+0.03 with frac clearly
   >0.55 across many eras).

**Why this supersedes the earlier 263-day result**
The original `backtest_latest.json` verdict (−0.072 Spearman on 263
days, mostly SIDEWAYS) was one era and under-powered. The cross-era
test corrects that methodological weakness and **confirms the same
conclusion**: momentum is dead in altcoins, era-independent.

**Consistency with the rest of the system**
- `trend_score` technical composite vs 7-day forward return: ~0 / weakly
  negative in `backtest_latest.json` (overall Spearman −0.072;
  hit-rate for score ≥60: precision 0.32, recall 0.10 — high scores fail
  to capture coins that actually rise).
- Regime mix skews heavily UNKNOWN (17,257 of 22,209 rows), so most of
  the sample is not regime-resolved.
- `entry_grade` is too new (2,163 rows, all in the last fold) to be
  judged yet — effectively untested.

**Status:** **NOT EDGE — do not build.** Momentum and technicals are now
falsified cross-era. This closes the three "literature-favourite"
candidates (momentum, technical trend score, and by extension anything
derived purely from price/volume history).

### Follow-up: does a *probability* framing change anything? — NO (tested)

The natural objection to the rank-based verdict is "what if we output a
**probability** (logistic model) instead of a rank score?" — as if a
probability were a new signal. It is not: probability is an **output
representation** of features, not new information. A logistic model on
the *same* momentum features was tested (17 Aug 2026,
`analysis/probability_momentum_test.py`, 111,900 rows / 35,180 OOS,
chronological split, features L7/L14/L30/L60):

| Target | OOS AUC | Verdict |
|--------|---------|---------|
| P(forward return > 0) — time-series | 0.525 | ~noise; only BULL 0.582 / BEAR 0.565, SIDEWAYS (bulk, n=26,836) **0.502** — regime direction, not selection edge |
| P(beats median return that day) — cross-sec | 0.498 | coin-flip |
| P(top-quintile return that day) — cross-sec | 0.508 | coin-flip |

The two **cross-sectional selection** targets (the question momentum was
supposed to answer — "which altcoin wins") are coin-flip (0.498 / 0.508).
The only >0.5 number is the time-series up/down in extreme regimes
(BULL/BEAR), which is capturing market *direction/volatility*, not
cross-sectional selection, and is absent in the dominant SIDEWAYS
regime. **Conclusion: re-encoding momentum as a probability adds nothing.**

A probability model is only worth building when the **features** carry
signal — and the only untested feature set is fundamental/VaF (needs
point-in-time history). The method (rank vs probability vs ML) is not the
lever; the information content of the features is.

---

## Fundamental / value-accrual — PENDING (accumulating point-in-time history)

**Corrected state (17 Aug 2026):** an earlier note claimed fundamentals
were "static, not persisted point-in-time" — that was **wrong**. The
collector has persisted per-(date, coin) fundamental features into
`history.db` snapshots since **2026-07-13** (tvl_growth_30d,
fee_accel_7v30, value_accrual_ratio, revenue_yield_ann, price_to_fees,
vaf score/otf/vfr). On 17 Aug this was **enriched** to also persist the
raw inputs (`fundamental_raw`: tvl_now, fees_7d/30d, revenue_30d,
holders_revenue) and `mcap` per date, so a future test is not hostage to
today's composite formula. All of it flows point-in-time into the
snapshot payload — no lookahead.

**Why it can't be judged yet:** only ~20 days have accumulated (16 DeFi/
infra coins). A fair cross-era test needs ≥60 days and ≥2 regimes with
sufficient samples.

**Ready-to-run framework:** `analysis/fundamental_value_test.py` reads
the point-in-time fundamental panel, joins forward 7d returns from
Binance Vision, and runs the same logistic / OOS-AUC cross-era test used
for momentum (targets: beats-median and top-quintile = "which coin
wins"). It reports **INSUFFICIENT DATA** honestly until history matures,
then produces the verdict. Run it periodically.

**Status:** the **only remaining open path**. A value test on this
feature set is the last candidate that can still be settled fairly. No
claim it works — just that it is finally testable once data accumulates.
If it too comes back ~0.5 cross-era, the terminal is honestly
intelligence-only (fundamental + macro + regime display), not a
predictor.

---

## Bottom line

| Signal | Status | Evidence |
|--------|--------|----------|
| Cross-sectional momentum (altcoins) | **REJECTED** | 9-yr × 14-coin era-split IC (table above) |
| Probability re-encoding of momentum | **REJECTED** | logistic OOS-AUC 0.498/0.508 cross-sec |
| trend_score technical composite | **REJECTED** | backtest_latest.json Spearman −0.072, low precision/recall |
| entry_grade | UNTESTED (insufficient rows) | 2,163 rows, one fold |
| Fundamental / VaF (point-in-time) | **PENDING — accumulating** | 20 days stored; framework ready, needs ≥60 days |

The terminal's honest position: it is strong as a **research / macro /
fundamental intelligence dashboard** (documented heuristics,
display-only news & correlation, coverage guardrails). It is **not** a
validated predictor. Do not market it as one.

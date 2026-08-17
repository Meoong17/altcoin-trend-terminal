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

---

## Fundamental / value-accrual — UNTESTED (data not yet sufficient)

**Status (17 Aug 2026):** cannot be fairly tested yet. The fundamental
and VaF inputs (TVL growth, revenue yield, fee acceleration, value
accrual from DeFiLlama) are static snapshots — they are not persisted
point-in-time per date, so there is no historical series to regress
against forward returns.

**What it would take to close this honestly**
1. Modify the collector to persist per-date point-in-time fundamental
   snapshots (not just current values).
2. Accumulate ~60+ days (ideally multiple regimes) of history.
3. Then run the same cross-era walk-forward IC / hit-rate test used
   above.

**Status:** open path — the only remaining candidate that can still be
settled fairly. Not a claim it works; just that it has not been
falsified (or confirmed) yet.

---

## Bottom line

| Signal | Status | Evidence |
|--------|--------|----------|
| Cross-sectional momentum (altcoins) | **REJECTED** | 9-yr × 14-coin era-split IC (table above) |
| trend_score technical composite | **REJECTED** | backtest_latest.json Spearman −0.072, low precision/recall |
| entry_grade | UNTESTED (insufficient rows) | 2,163 rows, one fold |
| Fundamental / VaF | UNTESTED (no point-in-time history) | data gap, not a negative result |

The terminal's honest position: it is strong as a **research / macro /
fundamental intelligence dashboard** (documented heuristics,
display-only news & correlation, coverage guardrails). It is **not** a
validated predictor. Do not market it as one.

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
| trend_score composite | **REJECTED as a picker** | 254-day point-in-time IC −0.0229 (t=−2.11); see audit below |
| trend_score v3 components (flow_rotation / participation / confirmation) | **UNTESTABLE YET — single era** | stored only since 2026-08-29 = 11 IC days |
| entry_grade / OTF | **NOT INDEPENDENT** | cross-sectional corr with trend_score 0.687; grade_rank IC +0.02 (35 days) |
| Fundamental / VaF (point-in-time) | **PENDING — accumulating** | 49/60 days; IC +0.22 on 42 days, orthogonality confirmed |
| trend_score in RISK_OFF / CAPITULATION only | **NEW HYPOTHESIS — not validated** | regime-conditional IC +0.104 (n=31) / +0.131 (n=14); see below |

## Model audit on accumulated point-in-time history (2026-09-23)

Tool: `analysis/model_audit.py` — cross-sectional Spearman IC of every live
score/component vs the forward 7d return, over the stored point-in-time
snapshots (`latest_price` sequence, no lookahead, no survivorship patch),
with era splits, regime splits, an inside-regime chronological half-split
(sign stability), and a same-day component correlation matrix.

Sample: 38,536 snapshot rows / 292 dates (2025-10-31 .. 2026-09-23);
34,348 rows have a 7d forward return; 254 dates carry enough coins for a
cross-section.

| target | days | mean IC | frac IC>0 | t | era1 / era2 / era3 |
|--------|------|---------|-----------|---|--------------------|
| trend_score | 254 | **−0.0229** | 0.441 | −2.11 | −0.026 / −0.052 / +0.028 |
| rel_strength | 254 | −0.0005 | 0.469 | −0.05 | +0.012 / −0.025 / +0.019 |
| compression | 231 | +0.0173 | 0.519 | +1.85 | +0.002 / +0.054 / −0.014 |
| flow_rotation | 11 | +0.1060 | 1.000 | +5.68 | n/a / n/a / +0.106 |
| participation | 11 | +0.0661 | 0.727 | +2.46 | n/a / n/a / +0.066 |
| confirmation | 11 | +0.2529 | 1.000 | +12.62 | n/a / n/a / +0.253 |
| otf | 35 | +0.0632 | 0.629 | +2.26 | n/a / n/a / +0.063 |
| vaf (fundamental) | 42 | **+0.2202** | 0.810 | +5.60 | n/a / n/a / +0.220 |
| grade_rank | 35 | +0.0199 | 0.400 | +0.79 | n/a / n/a / +0.020 |

Honest reading:
1. **trend_score is still dead as a picker** — mean IC negative, negative in
   2 of 3 eras, just below the noise band. This re-confirms the 9-year
   momentum verdict on the live formula, from the model's own stored rows.
2. **The three v3 core components cannot be judged yet.** They exist in the
   DB only from 2026-08-29 (coverage table: flow_rotation 0 rows before that
   month) — 11 IC days, ALL in the last era, one regime. Their juicy numbers
   (confirmation +0.25, flow_rotation +0.11) are exactly the overfit-bait the
   repo's rule exists to reject: `frac=1.0` on 11 days with a single
   unbroken market stretch is not evidence. **Do not touch weights for
   them.**
3. **VaF/fundamental remains the only candidate with a different story**:
   IC +0.22 on 42 days AND cross-sectional correlation with trend_score of
   only −0.15 on live `data.json` (407 coins) — i.e. it is scoring something
   the rejected technical family does not. Still single-era, still n≈42 days.
4. **entry_grade/OTF is largely a re-encoding of the rejected score.**
   Cross-sectionally corr(otf, trend_score) = **0.687** on live data, because
   all three auto OTF legs (market_structure, relative_strength,
   execution_quality) are built from price/volume features. grade_rank IC is
   +0.02. So "filter by Entry Timing" is not an independent, validated view —
   it inherits the rejected signal's ranking. This is a PRESENTATION-honesty
   problem, not a math bug; it must not be marketed as the validated filter.
5. **Internal double counting is real**: same-day component correlations
   `rel_strength|confirmation +0.571`, `participation|rel_strength +0.328`,
   `flow_rotation|participation +0.224`. The 8% "confirmation filter" is
   mostly a second helping of rel_strength.

### Regime reconstruction — and what it revealed

`history.db` carried a resolved regime on only **49 of 292 dates** (the
collector started writing it on 2026-07-13; every backfilled row is
UNKNOWN), so every regime-conditional question collapsed into one
17k-row UNKNOWN bucket. `analysis/regime_backfill.py` replays the model's
OWN `classify_regime` + `apply_hysteresis` rules over BTC daily closes to
fill that gap, writing to a NEW key `market_regime_reconstructed` and
**never overwriting** `market_regime` (the point-in-time record of what the
model actually knew that day is preserved; analysis opts in explicitly).

Validation of the method: of the 49 dates that DO have a stored live
regime, the reconstruction reproduces **45 (92%)** exactly while using only
the volatility + 30d-return legs. The 4 exceptions are explainable:
2026-07-31 (SIDEWAYS vs BULL_TREND), 2026-09-21/22 (live BULL_TREND held by
hysteresis vs reconstructed RISK_OFF), 2026-09-23 (no final BTC bar yet —
that date keeps its stored live value). Coverage: **49 → 291/292 dates**.
Caveat that must travel with any number below: `glf_score` and
`repo_stress` were never stored historically, so a RISK_OFF call the live
model made on repo stress alone cannot be reconstructed.

Regime-conditional IC (reconstructed), with inside-regime half-split:

| regime | n days | trend_score IC | frac>0 | half1 / half2 | verdict |
|--------|--------|----------------|--------|---------------|---------|
| RISK_OFF | 31 | **+0.104** | 0.71 | +0.169 / +0.043 | stable sign |
| CAPITULATION_RECOVERY | 14 | **+0.131** | 0.86 | n/a (<20) | suggestive only |
| SIDEWAYS | 96 | −0.080 | 0.34 | −0.129 / −0.030 | stable sign |
| BULL_TREND | 63 | −0.036 | 0.36 | −0.062 / −0.011 | stable sign |
| BEAR_TREND | 50 | −0.019 | 0.44 | −0.069 / +0.031 | SIGN FLIP |

`rel_strength` carries almost all of it (RISK_OFF +0.089, CAPITULATION
+0.120), which is economically coherent — in a selloff, the coins holding up
relative to BTC keep outperforming — and it is the same story that makes the
score ANTI-predictive in calm/trending tape (it rewards extended coins).

`vaf` by regime: RISK_OFF +0.385 (n=13), SIDEWAYS +0.198 (n=16),
BULL_TREND +0.083 (n=13) — **positive in every reconstructible regime**,
which is the one pattern in this audit that is regime-consistent.

**Status of the regime-conditional finding: HYPOTHESIS, not a verdict.**
n=31 RISK_OFF days spread over ~11 months, one asset class, one formula, and
the live regime carries repo-stress legs the reconstruction cannot see.
Per repo rule, no weight/blend change may be made on this basis; it needs a
walk-forward test on accumulated rows and a replication window. Recorded here
so it is not re-discovered as "new" next month.

## Engineering fixes shipped with this audit (2026-09-23)

1. **Micro-price corruption (class fix).** `closes_30d` (and MA/Bollinger/
   ATR/S-R/btc_ratio_latest) were written with `round(x, 6)`, which turns
   BTTC's ~3.8e-07 quote into **0.0** — a non-positive close that breaks the
   stated "zero non-positive closes" invariant, flattens the sparkline, and
   makes the client-side 24h change 0/0 = NaN. New `round_price()` (8
   significant digits) in `altcoin/analyzer.py`, used for every price-like
   field and in `altcoin/coinstats_fallback.py`. Verified: BTTC 30/30 closes
   positive in `data.json`; whole book **0 coins with non-positive closes**.
2. **Score-version attribution.** Rows were tagged `"v2-features"` even after
   the 2026-08 restructure replaced macro 10% with flow_rotation 28% +
   participation 28% — ~38k rows labeled with a formula they were not computed
   under. `SCORE_VERSION` in `altcoin/features.py` is now derived from a hash
   of `WEIGHTS` (`v3-coresignal-817dc2`), so any future weight change changes
   the tag automatically and eras stay attributable. (6 rows still showing
   `v2-features` on 2026-09-23 are leftovers from the earlier cycle whose
   symbols dropped out of the volume-ranked universe between runs — explained,
   not a live path.)
3. **Fundamental coverage 17 → 25 protocols.** The value test's bottleneck is
   point-in-time fundamental rows (only 16 coins). 8 slugs were verified live
   against DeFiLlama before being added to `DEFI_PROTOCOLS`:
   AERO/aerodrome, EIGEN/eigenlayer, SUSHI/sushiswap, YFI/yearn-finance,
   CVX/convex-finance, FXS/frax, 1INCH/1inch, SPK/spark (all `/protocol/{slug}`
   → 200 with non-trivial TVL, all returning non-None scores with populated
   fee/revenue legs). Rejected in the same pass: `ether-fi` and `linea`
   (HTTP 400), `pudgy-penguins` (TVL ~0), `resolv` (fees_30d = revenue_30d = 0
   — a TVL-only "score" is not value accrual). Panel now 24 coins/day.
4. **Open item needing a human decision — bStock-like tickers.**
   `_looks_like_unlisted_bstock()` (warning-only, by design) flags
   **ARMBUSDT** and **BNCBUSDT**. `ARMB` has already dropped out of the
   volume-ranked universe; `BNCBUSDT` is still live (~$7.7M 24h volume) and
   would be scored as if it were crypto. `MARSCOINUSDT` is a real memecoin,
   not a tokenized equity. Confirm whether ARMB/BNCB are Binance tokenized
   US equities before either is added to `_BSTOCKS_BASES` — the denylist must
   never be pattern-extended (genuine alts end in "B": ARB, BNB, SHIB, TRB).

## Re-run checklist

- `PYTHONPATH=. .venv/bin/python analysis/model_audit.py --json /tmp/audit.json`
  — re-run whenever new rows accumulate; watch the v3 components' day count
  climb past ~60 so they can finally be judged, and whether VaF's IC holds up
  in a second era.
- `PYTHONPATH=. .venv/bin/python analysis/regime_backfill.py` then `--write`
  — after any new backfill, to keep regime coverage complete.
- `PYTHONPATH=. .venv/bin/python analysis/fundamental_value_test.py` — flips
  from INSUFFICIENT DATA at 60 days of point-in-time fundamentals.


## trend_score redesign (core-signal) — CHANGE, NOT YET VALIDATED

On 2026-08 the trend_score composite was restructured per the audit +
user directive, and this **changes the score's meaning** — the old
backtest rejection above applies to the OLD weights, not automatically
to the new ones:

- **Before:** breakout 30% / rel-strength 30% / trend-consistency 20% /
  compression 10% / **macro 10%**. The macro 10% carried the GLOBAL
  GLF/repo value — identical across every coin in a snapshot, so it added
  zero cross-sectional ranking info (`analysis/macro_constant_test.py`
  confirmed: 75% of snapshot-days perfectly constant → rank impact 0).
- **After (core-signal):** flow_rotation 28% / participation 28% /
  rel-strength 24% / compression 12% / **confirmation 8%** (breakout +
  trend-consistency folded into one small filter). The constant macro is
  removed from the coin score; macro lives only in the regime/context
  layer (`regime.py`). Two new coin-specific drivers were added per user
  request: **participation** (volume building/participation) and
  **flow_rotation** (inflow/outflow money-before-price early trigger,
  proxied from volume vs price position — net-buy/exchange-flow legs are
  not free-tier and are honestly NOT faked).
- On current `data.json` the new formula re-ranks vs the old
  (Spearman≈0.62) and lowers the mean (≈48→≈37, because the constant
  inflation is gone and extended coins are now penalized).

**Status: NOT YET VALIDATED.** This is a weighting/component change, and
per repo rule it must be re-run through the walk-forward IC test on
*accumulated* point-in-time history before any predictive claim. The
volume-flow edge behind participation/flow_rotation was already tested
(`analysis/volume_flow_cascade_test.py`) and found **weak/conditional**
(early-accumulation gap ≈ +0.06 hit-rate inside a favorable gate, IC ≈ 0)
— i.e. honest expectation is these help as a *risk filter* (avoid
chasing extended) more than as a standalone picker. Re-run backtest and
re-settle once new point-in-time rows accumulate under the new formula.

The terminal's honest position remains: it is strong as a **research /
macro / fundamental intelligence dashboard** (documented heuristics,
display-only news & correlation, coverage guardrails). It is **not** a
validated predictor. Do not market it as one.

## Universe cleanup — tokenized bStocks removed (2026-08)

Binance's tokenized US-equities product ("bStocks", launched June 2026)
trades on the SAME spot API as crypto, so volume-ranked discovery
(EXTEND_TOP) silently pulled tokenized equities (Apple AAPLB, Amazon
AMZNB, AMD AMDB, SPY SPYB, SOXS SOXSB, SK Hynix SKHYB, etc.) into the
altcoin universe. These move on equity-market dynamics (earnings, Fed,
US hours), not crypto market structure — scoring them with a crypto
trend model compares a different asset class.

- `_BSTOCKS_BASES` in `altcoin/analyzer.py` expanded from 13 → ~63
  confirmed tokenized-equity tickers (grouped by Binance launch batch,
  June → Aug 2026), so `filter_alt_usdt_pairs` excludes them going forward.
- 24 bStocks purged from the live `data.json` (405 → 381 coins). Genuine
  crypto whose ticker happens to end in "B" were verified and KEPT
  (ARB/Arbitrum, BNB, SHIB, TRB/Tellor, BB, VIB, AMB/AirDAO,
  MOB/MobileCoin, YB). The defensive `_looks_like_unlisted_bstock()`
  heuristic remains warning-only (never auto-excludes) so a real new
  altcoin ending in "B" is not falsely dropped.
- **Note:** `history.db` still holds pre-cleanup bStock rows. Backtest
  numbers therefore still include tokenized-equity history (conservative,
  not a leak); a full purge is optional and deferred.

## Volume ≠ Flow — directional flow from taker-buy (2026-08)

Conceptual distinction (user directive): **volume** tells you there is
activity; **flow** answers WHO is pressing the market and WHICH WAY. The
previous `flow_rotation` was a proxy (volume-spike vs price position) —
still fundamentally a volume re-encode. This was replaced with a REAL
directional-flow signal:

- `fetch_klines` now captures Binance **takerBuyQuoteVolume** (kline
  index 10) — the quote volume executed by AGGRESSIVE BUYERS (takers
  hitting the ask). Public endpoint, no key.
- `_compute_flow_metrics()` derives the taker-buy SHARE per closed day
  (taker-buy / total), its 3d and 7d means, and `flow_trend` (3d/7d). A
  share >0.5 = buyers pressing; <0.5 = sellers. The change in the share is
  what flags the EARLY phase before price fully re-prices.
- `score_flow_rotation()` uses only these directional inputs, symmetric
  around a 50 neutral: strong AND rising buy pressure = early accumulation
  (high); sell pressure = below neutral. Verifed: buy-press rising →
  ~74, sell-press → ~41.
- **Honest data limits:** OKX/Bybit fallback tiers expose no taker-buy →
  flow is None and the component is excluded (weights renormalized), never
  fabricated. Backfill is technical-only and likewise has no flow.

**Status: NOT YET VALIDATED** — same as the rest of the v3 trend score.
The directional-flow component still needs the walk-forward IC test once
point-in-time history accumulates under it. Volume and flow remain two
separate drivers (participation 28% / flow_rotation 28%) — deliberately
NOT merged, so the model is sensitive to the early phase (who is pressing
in) without discarding activity information.




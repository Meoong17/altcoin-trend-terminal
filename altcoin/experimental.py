"""Parallel v4 candidate scores — RECORDED, NEVER RANKED (2026-09-23).

Why this exists
---------------
The external review archived at
`docs/reviews/chatgpt-trend-momentum-entry-2026-09-23.md` proposed replacing the
trend score with an acceleration/transition layer and decoupling entry quality
from the trend score. Those are HYPOTHESES, and the repo rule is that no scoring
change happens before walk-forward validation — with the v3 components at
n=11-17 days there is nothing to validate against yet.

So instead of adopting them, each cycle RECORDS them next to the live scores
under the `experimental` key, tagged EXP_VERSION. They are:

  * NOT read by any ranking, sort, grade, alert or dashboard element —
    nothing here can move a live number;
  * written to `data.json` and `history.db`, so the SAME audit machinery
    (`analysis/model_audit.py`: multi-horizon IC, quantile spread, path metrics)
    evaluates them against `trend_score` / `otf` / `vaf` on identical rows,
    point-in-time, the moment the sample is long enough.

The candidates, all computed from data the analyzer already fetches (no new
network call, no new dependency):

  accel_price        3-day price pace vs the average 3-day pace of the 7-day
                     window — is the move speeding up?
  accel_vol          3-day vs 10-day mean volume — is participation speeding up?
  accel_flow         buy_share_3d / buy_share_7d − 1 — is buy pressure speeding
                     up? (None on fallback tiers: no taker-buy field)
  transition         do the available accelerations AGREE in sign — the review's
                     "imbalance between layers" idea in its simplest form
  entry_decoupled    location-based entry quality (extension vs MA20, 30d range
                     position, reward:risk to 30d resistance/support) computed
                     WITHOUT touching trend_score — the review's point that an
                     entry readout must not re-encode the score it follows
  v4_equal           equal-weight mean of the available candidates: an UNFITTED
                     baseline, deliberately carrying zero fitted degrees of
                     freedom, so a later comparison cannot be accused of tuning

Every constant below is FIXED A PRIORI (round numbers, chosen before looking at
any IC) and is hashed into EXP_VERSION, so a stored row's formula stays
attributable — the lesson from SCORE_VERSION, where a hardcoded version string
that never moved with the formula destroyed the ability to date an era.
"""

import hashlib
import json

# All fixed a priori — no fitting, no tuning against outcomes.
CONSTANTS = {
    "accel_price_full": 0.10,   # +10pp of 3d pace vs the 7d average = full marks
    "accel_vol_full": 1.00,     # 3d volume at 2x the 10d mean = full marks
    "accel_flow_full": 0.15,    # buy-share 3d 15% above its 7d baseline = full
    "ext_tol": 0.05,            # extension vs MA20 tolerated before penalty
    "ext_span": 0.25,           # +25pp beyond tolerance = zero score
    "range_sweet": 0.60,        # preferred 30d range position
    "rr_full": 2.0,             # 2:1 reward:risk = full marks
}

EXP_VERSION = "v4exp-" + hashlib.md5(
    json.dumps(CONSTANTS, sort_keys=True).encode()).hexdigest()[:6]

TOTAL_CANDIDATES = 6


def _clip01(x):
    return max(0.0, min(1.0, x))


def _lin(value, full):
    """Map a signed raw value onto 0-100 with 0 -> 50 (full -> 100)."""
    if value is None:
        return None
    return round(_clip01(0.5 + value / (2.0 * full)) * 100.0, 2)


def _mean_or_none(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 2)


def score_experimental(closes, quote_volumes, flow, feats, ta):
    """-> dict of parallel candidate scores + raw inputs + coverage.

    `closes` and `quote_volumes` are the analyzer's oldest-first series (closes
    include the in-progress candle; volumes are closed-only, matching
    `_compute_volume_metrics`). `flow` may be None on fallback tiers. Never
    raises: a candidate whose inputs are missing is None, never a fabricated 0.
    """
    closes = closes or []
    vols = list(quote_volumes or [])[:-1] or []   # closed candles only
    feats = feats or {}
    ta = ta or {}
    flow = flow or {}

    # ── price acceleration: 3d pace vs the average 3d pace of the 7d window
    ret3 = ret7 = accel_p = None
    if len(closes) >= 8 and closes[-4] and closes[-8]:
        ret3 = closes[-1] / closes[-4] - 1.0
        ret7 = closes[-1] / closes[-8] - 1.0
        accel_p = ret3 - ret7 * (3.0 / 7.0)

    # ── volume acceleration: 3d mean vs 10d mean
    accel_v = None
    if len(vols) >= 10:
        v3 = sum(vols[-3:]) / 3.0
        v10 = sum(vols[-10:]) / 10.0
        if v10 > 0:
            accel_v = v3 / v10 - 1.0

    # ── flow acceleration: buy-share 3d vs its own 7d baseline
    ftrend = flow.get("flow_trend")
    accel_f = (ftrend - 1.0) if isinstance(ftrend, (int, float)) else None

    # ── transition: agreement of the available acceleration signs
    signs = [1 if v > 0 else (-1 if v < 0 else 0)
             for v in (accel_p, accel_v, accel_f) if v is not None]
    transition = None
    if len(signs) >= 2:
        transition = round((sum(signs) / len(signs) + 1.0) / 2.0 * 100.0, 2)

    # ── entry quality WITHOUT trend_score: location + reward:risk only
    ext = None
    ma20 = ta.get("ma20")
    if ma20 and closes:
        ext = closes[-1] / ma20 - 1.0
    ext_score = (1.0 - _clip01(max(0.0, ext - CONSTANTS["ext_tol"])
                               / CONSTANTS["ext_span"])
                 if ext is not None else None)

    rg = feats.get("prox_30d_high")
    rg_score = (_clip01(1.0 - abs(rg - CONSTANTS["range_sweet"]) / CONSTANTS["range_sweet"])
                if isinstance(rg, (int, float)) else None)

    rr = rr_score = None
    px = closes[-1] if closes else None
    sup = (ta.get("support") or {}).get("30d")
    res = (ta.get("resistance") or {}).get("30d")
    if px and sup and res and px > sup:
        reward = res / px - 1.0
        risk = px / sup - 1.0
        if risk > 0:
            rr = reward / risk
            rr_score = _clip01(rr / CONSTANTS["rr_full"])
    entry = _mean_or_none([None if s is None else s * 100.0
                           for s in (ext_score, rg_score, rr_score)])

    candidates = {
        "accel_price": _lin(accel_p, CONSTANTS["accel_price_full"]),
        "accel_vol": _lin(accel_v, CONSTANTS["accel_vol_full"]),
        "accel_flow": _lin(accel_f, CONSTANTS["accel_flow_full"]),
        "transition": transition,
        "entry_decoupled": entry,
        "v4_equal": None,
    }
    v4 = _mean_or_none([candidates["accel_price"], candidates["accel_vol"],
                        candidates["accel_flow"], candidates["transition"],
                        candidates["entry_decoupled"]])
    candidates["v4_equal"] = v4

    return {
        "version": EXP_VERSION,
        "status": "EXPERIMENTAL — recorded only, never ranked or displayed as a signal",
        "candidates": candidates,
        "coverage": {
            "used": sum(1 for v in candidates.values() if v is not None),
            "total": TOTAL_CANDIDATES,
            "weight_covered": round(sum(1 for v in candidates.values()
                                        if v is not None) / TOTAL_CANDIDATES, 3),
        },
        # raw inputs kept for forensics: the mapping above can be re-derived
        # later without re-fetching anything or trusting a rounded score
        "raw": {
            "ret_3d": None if ret3 is None else round(ret3, 6),
            "ret_7d": None if ret7 is None else round(ret7, 6),
            "accel_price": None if accel_p is None else round(accel_p, 6),
            "accel_vol": None if accel_v is None else round(accel_v, 6),
            "flow_trend": ftrend,
            "accel_flow": None if accel_f is None else round(accel_f, 6),
            "extension_vs_ma20": None if ext is None else round(ext, 6),
            "prox_30d_high": rg,
            "rr_30d": None if rr is None else round(rr, 4),
        },
    }

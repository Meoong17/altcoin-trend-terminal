"""
Direct Value Accrual Framework (VaF) v1.0 — engine implementation.

The framework structure is LOCKED per the source document:

    VaF /150   = PQ /50 + NF /50 + RG /50
    Actual /100 = PQ + NF
    OTF /50     separate timing framework (never changes VaF)
    VFR         non-scored 30D confirmation ratio with quality label
    Chain: PQ -> NF -> RG -> VaF -> OTF -> VFR -> Guardrails ->
           Confidence -> Verdict -> Action

What this engine adds is a PROVENANCE system, because VaF was written
for a human analyst and several metrics are judgment calls that must
not be faked by a formula:

    auto    computed from exact data (DeFiLlama fees/revenue/holders-
            revenue/TVL, Binance price/volume, market cap)
    proxy   computed, but the mapping from data to band is heuristic
    manual  supplied via vaf_overrides.json (the analyst's judgment)
    n/a     no data and no override — weight EXCLUDED from the pillar,
            pillar reported with its coverage %, and Confidence degraded

Pillar scores are reported two ways:
    raw        sum of scored metrics only (honest partial sum)
    scaled     raw projected onto /50 using only covered weight
Verdicts above watch-tier require coverage >= 70% — an anti-inflation
guardrail in the spirit of the document's cap rules: an auto-only run
can nominate, but cannot crown, an Elite candidate.

Metric-by-metric data mapping (weights exactly as locked):

  PQ /50
    economic_scale   /12  auto   peer-percentile of 30d fees + TVL within
                                 the tracked DeFi universe (peer-relative,
                                 per the document's technique note)
    product_market_fit /10 manual
    moat             /12  manual
    revenue_durability /10 proxy  fee acceleration stability (7d run-rate
                                 vs 30d) — durability needs longer history
                                 than free endpoints give, hence proxy
    execution_risk   /6   manual
  NF /50
    live_value_capture /18 auto  holders-revenue: existence + share of
                                 revenue reaching holders (buyback/burn/
                                 fee-share measured, not narrated)
    token_demand_link  /12 manual
    flow_coverage      /10 proxy holders-rev magnitude & share (structural
                                 emissions data not free-tier reliable)
    supply_cleanliness /6  manual (unlock schedules not free-tier)
    reliability        /4  proxy holders-rev present in BOTH 7d and 30d
                                 windows -> recurring, not a one-off spike
                                 (guardrail 6: spike must not create NF)
  RG /50
    forward_growth_delta /10 proxy TVL 30d growth + fee acceleration
    value_capture_expansion /12 manual
    catalyst_strength    /8  manual
    valuation_dislocation /14 auto inverted peer-percentile of P/F ratio
    repricing_friction   /6  manual
  OTF /50
    market_structure   /12 auto  score-v2 breakout + trend-consistency
    relative_strength  /10 auto  IR_7d vs BTC (document: most relevant
                                 benchmark; BTC is this system's baseline)
    catalyst_timing    /10 manual
    supply_timing      /8  manual
    execution_quality  /10 proxy pullback-vs-extension position + vol

  VFR = 30d token-positive value / 30d dilution.
    Numerator: holders-revenue 30d (auto, exact from DeFiLlama).
    Denominator: dilution requires unlock/emission data -> override-only.
    Without an override the display is "N/A" with quality "N/A", exactly
    per the document's display convention — never a fabricated ratio.

Overrides file (repo root, vaf_overrides.json):
{
  "AAVEUSDT": {
    "pq": {"moat": 10, "product_market_fit": 8, "execution_risk": 5},
    "nf": {"token_demand_link": 8, "supply_cleanliness": 4},
    "rg": {"value_capture_expansion": 9, "catalyst_strength": 5,
            "repricing_friction": 4},
    "otf": {"catalyst_timing": 6, "supply_timing": 6},
    "vfr": {"dilution_30d_usd": 4200000, "quality": "Proxy"},
    "flags": {"unlock_within_3m": false, "fee_switch_only": false,
               "chart_breakdown": false},
    "confidence": "B",
    "notes": "ve-lock fee share live; GHO growth thesis"
  }
}
Any manual metric must respect its locked weight; values are clamped.
"""

import json
import os

PILLARS = {
    "pq": {"economic_scale": 12, "product_market_fit": 10, "moat": 12,
           "revenue_durability": 10, "execution_risk": 6},
    "nf": {"live_value_capture": 18, "token_demand_link": 12,
           "flow_coverage": 10, "supply_cleanliness": 6, "reliability": 4},
    "rg": {"forward_growth_delta": 10, "value_capture_expansion": 12,
           "catalyst_strength": 8, "valuation_dislocation": 14,
           "repricing_friction": 6},
    "otf": {"market_structure": 12, "relative_strength": 10,
            "catalyst_timing": 10, "supply_timing": 8,
            "execution_quality": 10},
}

VAF_TIERS = [(120, "Elite VaF"), (114, "Elite Candidate"), (105, "Strong VaF"),
             (95, "High Watch"), (80, "Thesis-Dependent"), (65, "Speculative"),
             (0, "Weak / Broken Thesis")]

OVERRIDES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vaf_overrides.json")

MIN_COVERAGE_FOR_ELITE = 0.70


def _m(weight, frac, provenance, note=None):
    """One scored metric: fraction of weight per the universal bands."""
    frac = max(0.0, min(1.0, frac))
    return {"score": round(weight * frac, 1), "weight": weight,
            "provenance": provenance, **({"note": note} if note else {})}


def pct_of_peers(value, peer_values):
    vals = [v for v in peer_values if v is not None]
    if value is None or not vals:
        return None
    return sum(1 for v in vals if v <= value) / len(vals)


# ── Auto/proxy metric scorers ──

def score_economic_scale(raw, peer_raws):
    fees = (raw or {}).get("fees_30d")
    tvl = (raw or {}).get("tvl_now")
    p_f = pct_of_peers(fees, [r.get("fees_30d") for r in peer_raws])
    p_t = pct_of_peers(tvl, [r.get("tvl_now") for r in peer_raws])
    ps = [p for p in (p_f, p_t) if p is not None]
    if not ps:
        return None
    return _m(12, sum(ps) / len(ps), "auto", "peer pct of 30d fees & TVL")


def score_revenue_durability(raw):
    f7, f30 = (raw or {}).get("fees_7d"), (raw or {}).get("fees_30d")
    if not f30 or f7 is None:
        return None
    accel = (f7 / 7.0) / (f30 / 30.0)
    # stable-to-growing run-rate reads as durable; collapsing run-rate
    # (accel << 1) is the free-tier visible symptom of fragile revenue
    frac = 0.70 if 0.9 <= accel <= 1.4 else (0.5 if accel > 1.4 else
           max(0.1, 0.7 - (0.9 - accel)))
    return _m(10, frac, "proxy", f"fee run-rate 7d/30d = {accel:.2f}")


def score_live_value_capture(raw):
    hrev, rev = (raw or {}).get("holders_revenue_30d"), (raw or {}).get("revenue_30d")
    if hrev is None:
        return None
    if not rev or rev <= 0 or hrev <= 0:
        return _m(18, 0.08, "auto", "no measured value reaching holders")
    share = min(1.0, hrev / rev)
    # bands: >=80% of revenue reaching holders is elite direct capture
    frac = 0.93 if share >= 0.8 else 0.80 if share >= 0.5 else \
           0.65 if share >= 0.25 else 0.45 if share >= 0.1 else 0.30
    return _m(18, frac, "auto", f"holders-rev share {share:.0%} of revenue")


def score_flow_coverage(raw):
    hrev = (raw or {}).get("holders_revenue_30d")
    rev = (raw or {}).get("revenue_30d")
    if hrev is None or not rev:
        return None
    share = min(1.0, hrev / rev) if rev > 0 else 0.0
    return _m(10, 0.3 + 0.5 * share, "proxy",
              "structural coverage proxied by holders-rev share; emissions not free-tier")


def score_reliability(raw):
    h7, h30 = (raw or {}).get("holders_revenue_7d"), (raw or {}).get("holders_revenue_30d")
    if h30 is None:
        return None
    if h30 <= 0:
        return _m(4, 0.1, "proxy", "no live mechanism observed")
    recurring = h7 is not None and h7 > 0
    return _m(4, 0.65 if recurring else 0.4, "proxy",
              "present in both 7d and 30d windows" if recurring else "30d only")


def score_forward_growth(raw):
    t_now, t_prev = (raw or {}).get("tvl_now"), (raw or {}).get("tvl_30d_ago")
    f7, f30 = (raw or {}).get("fees_7d"), (raw or {}).get("fees_30d")
    parts = []
    if t_now and t_prev:
        parts.append(max(0.0, min(1.0, 0.5 + (t_now / t_prev - 1) * 2.5)))
    if f30 and f7 is not None:
        parts.append(max(0.0, min(1.0, 0.5 + ((f7 / 7) / (f30 / 30) - 1) * 1.2)))
    if not parts:
        return None
    return _m(10, sum(parts) / len(parts), "proxy", "TVL & fee growth trajectory")


def score_valuation_dislocation(raw, mcap, peer_pf):
    f30 = (raw or {}).get("fees_30d")
    if not (mcap and f30):
        return None
    pf = mcap / (f30 * 365 / 30)
    p = pct_of_peers(pf, peer_pf)
    if p is None:
        return None
    return _m(14, 1.0 - p, "auto", f"P/F {pf:.1f}x, cheaper than {100*(1-p):.0f}% of peers")


def score_market_structure(trend_drivers):
    comps = {d["component"]: d["value"] for d in (trend_drivers or [])}
    vals = [comps[k] for k in ("breakout", "trend_consistency") if k in comps]
    if not vals:
        return None
    return _m(12, (sum(vals) / len(vals)) / 100.0, "auto", "score-v2 breakout + trend")


def score_relative_strength(features):
    ir = (features or {}).get("ir_7d")
    if ir is None:
        return None
    frac = 0.95 if ir >= 2 else 0.75 if ir >= 1 else 0.55 if ir >= 0 else \
           0.35 if ir >= -1 else 0.15
    return _m(10, frac, "auto", f"IR_7d vs BTC = {ir:+.2f}")


def score_execution_quality(features):
    prox = (features or {}).get("prox_30d_high")
    if prox is None:
        return None
    # constructive pullback zone scores best; deeply broken or fully
    # extended chases score worst — proxy for entry/invalidation quality
    frac = 0.75 if 0.55 <= prox <= 0.9 else 0.55 if prox > 0.9 else \
           0.45 if prox >= 0.35 else 0.25
    return _m(10, frac, "proxy", f"position in 30d range: {prox:.0%}")


# ── Pillar assembly, caps, verdict ──

def build_pillar(name, autos, overrides):
    weights = PILLARS[name]
    metrics = {}
    for metric, w in weights.items():
        ov = (overrides or {}).get(name, {}).get(metric)
        if ov is not None:
            metrics[metric] = {"score": max(0.0, min(float(w), float(ov))),
                               "weight": w, "provenance": "manual"}
        elif autos.get(metric) is not None:
            metrics[metric] = autos[metric]
        else:
            metrics[metric] = {"score": None, "weight": w, "provenance": "n/a"}
    scored = {k: v for k, v in metrics.items() if v["score"] is not None}
    cov_w = sum(v["weight"] for v in scored.values())
    raw = sum(v["score"] for v in scored.values())
    return {
        "metrics": metrics,
        "raw": round(raw, 1),
        "scaled": round(raw * 50.0 / cov_w, 1) if cov_w else None,
        "coverage": round(cov_w / 50.0, 2),
    }


def apply_caps(pillars, raw, flags):
    """Checkable subset of the document's cap rules, with reasons."""
    caps = []
    nf, rg, otf = pillars["nf"], pillars["rg"], pillars["otf"]
    hrev = (raw or {}).get("holders_revenue_30d")
    if hrev is not None and hrev <= 0 and nf["scaled"] is not None and nf["scaled"] > 15:
        nf["scaled"] = 15.0
        caps.append("NF capped 15: protocol revenue does not reach token")
    if (flags or {}).get("fee_switch_only") and nf["scaled"] is not None and nf["scaled"] > 18:
        nf["scaled"] = 18.0
        caps.append("NF capped 18: future fee switch only")
    if (flags or {}).get("unlock_within_3m") and rg["scaled"] is not None and rg["scaled"] > 35:
        rg["scaled"] = 35.0
        caps.append("RG capped 35: major unlock within 3 months")
    ms = pillars["otf"]["metrics"].get("market_structure", {}).get("score")
    if ((flags or {}).get("chart_breakdown") or (ms is not None and ms <= 3)) \
            and otf["scaled"] is not None and otf["scaled"] > 25:
        otf["scaled"] = 25.0
        caps.append("OTF capped 25: chart in breakdown")
    return caps


def compute_vfr(raw, override_vfr):
    hrev = (raw or {}).get("holders_revenue_30d")
    dil = (override_vfr or {}).get("dilution_30d_usd")
    quality = (override_vfr or {}).get("quality", "Proxy") if dil is not None else "N/A"
    if hrev is None:
        return {"display": "N/A", "quality": "N/A", "read": "needs manual verification"}
    if dil is None:
        if hrev > 0:
            return {"display": "N/A", "quality": "N/A",
                    "read": "numerator live (holders-rev 30d > 0); dilution unverified"}
        return {"display": "Weak / no capture", "quality": "Directional",
                "read": "no token-positive value measured"}
    if dil <= 0:
        return {"display": "Positive / near-zero dilution" if hrev > 0 else "N/A",
                "quality": quality, "read": "confirming"}
    ratio = hrev / dil
    disp = f"{'+' if ratio >= 1 else '-'}{(ratio if ratio >= 1 else 1/ratio):.1f}x"
    read = ("confirming" if ratio >= 1.5 else "mildly confirming" if ratio >= 1.0
            else "mildly contradicting" if ratio >= 1/1.5 else "contradicting")
    return {"display": disp, "quality": quality, "read": read,
            "numerator_30d": hrev, "dilution_30d": dil}


def _vfr_positive(vfr):
    return vfr["display"].startswith("+") or vfr["display"].startswith("Positive")


def confidence_grade(pillars, vfr, override_conf=None):
    cov = [p["coverage"] for p in pillars.values() if p["scaled"] is not None]
    avg = sum(cov) / len(cov) if cov else 0.0
    grade = "B" if avg >= 0.85 else "C" if avg >= 0.55 else "D"
    if vfr["quality"] == "N/A" and grade == "B":
        grade = "C"  # proxy-heavy read per the document's confidence examples
    if override_conf in ("A", "B", "C", "D"):
        # the analyst's grade is honored, but auto-detected data gaps can
        # only WORSEN confidence, never improve it ('A' < 'D' lexically
        # matches best -> worst, so worse == max)
        grade = max(grade, override_conf)
    return grade


def verdict_action(vaf, actual, otf, vfr, coverage_ok):
    pos = _vfr_positive(vfr)
    very_neg = vfr["display"].startswith("-") and vfr["display"] not in ("-1.0x",)
    if vaf is None or otf is None:
        return "Insufficient Data", "Wait for Confirmation"
    if not coverage_ok and vaf >= 95:
        return "Elite Watchlist (coverage-limited)", "Watch for Entry"
    if vaf >= 120 and pos and otf >= 35:
        return "Elite Accumulation Candidate", "Accumulate"
    if vaf >= 120:
        return "Elite Watchlist", "Watch for Entry" if otf < 35 else "Wait for Confirmation"
    if 114 <= vaf < 120 and pos and otf >= 35:
        return "Elite Candidate", "Accumulate"
    if 105 <= vaf < 114 and otf >= 35:
        return "Strong Candidate", "Accumulate" if pos else "Wait for Confirmation"
    if 95 <= vaf < 105 and otf >= 35:
        return "High Watch / Selective", "Watch for Entry"
    if 80 <= vaf < 95:
        return "Thesis-Dependent / Trade Only", "Trade Only" if otf >= 35 else "Wait for Confirmation"
    if very_neg and otf < 28:
        return "Wait / Avoid", "Avoid"
    if vaf < 80 and otf >= 41:
        return "Tactical Trade Only", "Trade Only"
    return "Weak / Broken Token Economics" if vaf < 65 else "Speculative", "Avoid" if vaf < 65 else "Wait for Confirmation"


def load_overrides(path=None):
    p = path or OVERRIDES_PATH
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            return json.load(f)
    except (ValueError, OSError):
        return {}


def evaluate_token(symbol, raw, mcap, features, trend_drivers,
                   peer_raws, peer_pf, overrides=None):
    """Full VaF chain for one token -> master-table row dict."""
    ov = (overrides or {}).get(symbol, {})
    autos = {
        # PQ
        "economic_scale": score_economic_scale(raw, peer_raws),
        "revenue_durability": score_revenue_durability(raw),
        # NF
        "live_value_capture": score_live_value_capture(raw),
        "flow_coverage": score_flow_coverage(raw),
        "reliability": score_reliability(raw),
        # RG
        "forward_growth_delta": score_forward_growth(raw),
        "valuation_dislocation": score_valuation_dislocation(raw, mcap, peer_pf),
        # OTF
        "market_structure": score_market_structure(trend_drivers),
        "relative_strength": score_relative_strength(features),
        "execution_quality": score_execution_quality(features),
    }
    pillars = {name: build_pillar(name, autos, ov) for name in PILLARS}
    caps = apply_caps(pillars, raw, ov.get("flags"))

    pq, nf, rg, otf = (pillars[k]["scaled"] for k in ("pq", "nf", "rg", "otf"))
    actual = round(pq + nf, 1) if None not in (pq, nf) else None
    vaf = round(pq + nf + rg, 1) if None not in (pq, nf, rg) else None
    tier = next((t for lo, t in VAF_TIERS if vaf is not None and vaf >= lo), None)

    vfr = compute_vfr(raw, ov.get("vfr"))
    core_cov = [pillars[k]["coverage"] for k in ("pq", "nf", "rg")]
    coverage_ok = min(core_cov) >= MIN_COVERAGE_FOR_ELITE if core_cov else False
    conf = confidence_grade(pillars, vfr, ov.get("confidence"))
    verdict, action = verdict_action(vaf, actual, otf, vfr, coverage_ok)

    return {
        "symbol": symbol,
        "pq": pq, "nf": nf, "rg": rg, "actual": actual,
        "vaf": vaf, "tier": tier, "otf": otf,
        "vfr": vfr, "confidence": conf,
        "verdict": verdict, "action": action,
        "caps_applied": caps,
        "coverage": {k: pillars[k]["coverage"] for k in PILLARS},
        "pillars": pillars,
        "notes": ov.get("notes"),
    }

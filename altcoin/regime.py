"""
Market regime detection — deterministic proxy for the blueprint's HMM.

The blueprint (section 3) specifies a 5-state GaussianHMM. That is
deliberately NOT implemented yet, for reasons flagged in review:
    - ~730 daily points estimating a 5-state Gaussian HMM is unstable
      (state label-switching between retrains),
    - Viterbi decoding over full history is smoothed — it uses future
      data, a classic look-ahead leak when states are consumed as
      features at time t.

Instead: transparent threshold rules over the same inputs the HMM would
see, producing the SAME five state names, so a future HMM can be A/B
tested against this proxy without changing any downstream contract.
This mirrors the Meridian principle: deterministic constraints first,
learned models only once they beat the deterministic baseline.

Inputs available free-tier: BTC daily closes (>=90d) plus the macro
layer already computed each cycle (GLF score, repo stress).
"""

THRESHOLDS = {
    "capitulation_ret30": -0.25,   # BTC 30d return below this ...
    "capitulation_volp": 85,       # ... with realized vol in top 15% of 90d
    "riskoff_volp": 85,            # vol percentile alone
    "riskoff_repo": 0.70,          # or funding-market stress
    "bull_ret30": 0.08,
    "bear_ret30": -0.08,
    "glf_tailwind": 60,            # GLF above = liquidity supports BULL call
}

STATES = ["BULL_TREND", "BEAR_TREND", "SIDEWAYS", "RISK_OFF", "CAPITULATION_RECOVERY"]


def realized_vol_series(closes, window=14):
    """Rolling annualization-free stdev of daily returns."""
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    out = []
    for i in range(window, len(rets) + 1):
        w = rets[i - window:i]
        m = sum(w) / window
        out.append((sum((r - m) ** 2 for r in w) / window) ** 0.5)
    return out


def classify_regime(btc_closes, glf_score=None, repo_stress=None, t=THRESHOLDS):
    """
    -> {"state": ..., "reasons": [...], "inputs": {...}} or state UNKNOWN
    when there isn't enough BTC history. Rules are ordered by severity:
    capitulation > risk-off > bear > bull > sideways, so overlapping
    conditions resolve to the more defensive state.
    """
    if not btc_closes or len(btc_closes) < 60:
        return {"state": "UNKNOWN", "reasons": ["insufficient BTC history"], "inputs": {}}

    ret30 = btc_closes[-1] / btc_closes[-31] - 1 if len(btc_closes) >= 31 else None
    vols = realized_vol_series(btc_closes)
    volp = None
    if len(vols) >= 31:
        cur, hist = vols[-1], vols[-91:-1]
        volp = 100.0 * sum(1 for h in hist if h <= cur) / len(hist)

    inputs = {"btc_ret_30d": round(ret30, 4) if ret30 is not None else None,
              "btc_volp_90d": round(volp, 1) if volp is not None else None,
              "glf_score": glf_score, "repo_stress": repo_stress}
    reasons = []

    if ret30 is not None and volp is not None and \
            ret30 < t["capitulation_ret30"] and volp > t["capitulation_volp"]:
        reasons.append(f"BTC 30d {ret30:+.0%} with vol p{volp:.0f}")
        return {"state": "CAPITULATION_RECOVERY", "reasons": reasons, "inputs": inputs}

    if (volp is not None and volp > t["riskoff_volp"]) or \
            (repo_stress is not None and repo_stress > t["riskoff_repo"]):
        if volp is not None and volp > t["riskoff_volp"]:
            reasons.append(f"BTC vol p{volp:.0f} > {t['riskoff_volp']}")
        if repo_stress is not None and repo_stress > t["riskoff_repo"]:
            reasons.append(f"repo stress {repo_stress:.2f} > {t['riskoff_repo']}")
        return {"state": "RISK_OFF", "reasons": reasons, "inputs": inputs}

    if ret30 is not None and ret30 < t["bear_ret30"]:
        reasons.append(f"BTC 30d {ret30:+.0%}")
        return {"state": "BEAR_TREND", "reasons": reasons, "inputs": inputs}

    if ret30 is not None and ret30 > t["bull_ret30"]:
        reasons.append(f"BTC 30d {ret30:+.0%}")
        if glf_score is not None and glf_score >= t["glf_tailwind"]:
            reasons.append(f"GLF {glf_score:.0f} tailwind")
        return {"state": "BULL_TREND", "reasons": reasons, "inputs": inputs}

    reasons.append("no threshold triggered")
    return {"state": "SIDEWAYS", "reasons": reasons, "inputs": inputs}


SEVERITY = {"CAPITULATION_RECOVERY": 4, "RISK_OFF": 3, "BEAR_TREND": 2,
            "BULL_TREND": 1, "SIDEWAYS": 0, "UNKNOWN": -1}


def apply_hysteresis(new_regime, prev_state, days_in_prev, min_dwell=6):
    """
    Anti-flapping: a regime change only takes effect after the previous
    state has lived >= min_dwell days — EXCEPT when the new state is more
    severe (defensive states switch immediately; you never want hysteresis
    delaying a RISK_OFF call). Returns the regime dict, annotated with
    "held": True + the pending state when the switch is suppressed.
    """
    new_state = new_regime.get("state", "UNKNOWN")
    if not prev_state or prev_state == new_state or new_state == "UNKNOWN":
        return new_regime
    if SEVERITY.get(new_state, 0) > SEVERITY.get(prev_state, 0):
        return new_regime  # escalation is never delayed
    if days_in_prev is not None and days_in_prev < min_dwell:
        held = dict(new_regime)
        held["state"] = prev_state
        held["held"] = True
        held["pending"] = new_state
        held["reasons"] = [f"hysteresis: {prev_state} held (day {days_in_prev}/{min_dwell}),"
                           f" pending {new_state}"] + new_regime.get("reasons", [])
        return held
    return new_regime

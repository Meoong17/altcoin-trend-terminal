"""Does a PROBABILITY framing rescue altcoin momentum? — empirical test.

The cross-sectional momentum VERDICT rejected rank-based momentum (IC ~0
cross-era). This script tests the common follow-up idea: "what if instead
of a rank score we output a *probability* of a coin being a winner, via a
logistic / probit-style model on the same momentum features?"

Key methodological point this tests: a probability is an OUTPUT
representation, not new information. If the input features (trailing
returns) carry no information about forward returns, a calibrated
probability model built on those SAME features must also be ~0.5 / no
edge. This script demonstrates that empirically with proper
chronological (walk-forward-ish) splits and out-of-sample AUC, rather
than asserting it.

Two targets:
  A) TIME-SERIES:  P(forward 7d return > 0)            (classify up vs down)
  B) CROSS-SEC:    P(coin in top-quintile of forward returns that day)
                   (classify "will be a winner" cross-sectionally)

Model: binary logistic regression (numpy, L2), standardized features
[L7, L14, L30, L60 trailing returns]. Fit on chronological training
dates, evaluate AUC on held-out test dates (never overlapping). Reports
per-regime AUC on the test set too.

Data source: Binance Vision monthly 1d klines, cached in /tmp/bv_monthly_cache
(reused from momentum_crossera_test.py). Same 14-coin universe.
"""
import os, datetime
from collections import defaultdict
from statistics import mean

CACHE = "/tmp/bv_monthly_cache"
COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT", "AAVEUSDT", "UNIUSDT",
         "CRVUSDT", "SNXUSDT", "COMPUSDT", "MKRUSDT", "DOGEUSDT", "RAYUSDT",
         "DYDXUSDT", "LDOUSDT"]
LOOKBACKS = [7, 14, 30, 60]
HORIZON = 7

def _normalize_ts(ts):
    if ts > 1e14:          # microseconds
        return ts / 1000
    if ts > 1e11:          # milliseconds
        return ts
    return ts * 1000       # seconds

def _read_csv(fn):
    rows = []
    with open(fn) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            t, close = line.split(",")
            rows.append((_normalize_ts(int(t)), float(close)))
    return rows

def _fmt(ts):
    return datetime.datetime.fromtimestamp(ts/1000, datetime.timezone.utc).date().isoformat()

# --- Rebuild daily close series per symbol from cache ---
series = {}
for sym in COINS:
    by_ts = {}
    # scan cache files
    prefix = f"{sym}-"
    for fn in os.listdir(CACHE):
        if fn.startswith(prefix) and fn.endswith(".csv"):
            for t, c in _read_csv(os.path.join(CACHE, fn)):
                by_ts[t] = c
    series[sym] = sorted(by_ts.items())
    print(f"  {sym}: {len(series[sym])} closes {_fmt(series[sym][0][0])}..{_fmt(series[sym][-1][0])}")

btc_dates = [t for t, _ in series["BTCUSDT"]]
prices = {sym: dict(s) for sym, s in series.items()}
def price_at(sym, ts):
    d = prices.get(sym)
    return d.get(ts) if d else None

def btc_ret30(idx):
    i0 = max(0, idx-30)
    p0 = price_at("BTCUSDT", btc_dates[i0]); p1 = price_at("BTCUSDT", btc_dates[idx])
    if p0 and p1 and p0 > 0:
        return p1/p0 - 1
    return None

# --- Build feature/label matrix (one row per (date, coin)) ---
# For cross-sectional target, quintile/median computed within the date cross-section.
import numpy as np

rows = []  # (date_idx, regime, feature_vector, fwd_ret)
for i in range(40, len(btc_dates) - HORIZON):
    t = btc_dates[i]; t_fwd = btc_dates[i + HORIZON]
    r30 = btc_ret30(i)
    if r30 is None:
        continue
    regime = "BULL" if r30 > 0.15 else ("BEAR" if r30 < -0.15 else "SIDEWAYS")
    for L in LOOKBACKS:
        i0 = i - L
        if i0 < 0:
            continue
        t0 = btc_dates[i0]
        feats = []
        for sym in COINS:
            p0 = price_at(sym, t0); p1 = price_at(sym, t); pf = price_at(sym, t_fwd)
            if p0 and p1 and pf and p0 > 0 and p1 > 0:
                feats.append((sym, p1/p0 - 1, pf/p1 - 1))
        if len(feats) < 8:
            continue
        med = float(np.median([fr for _, _, fr in feats]))
        q80 = float(np.quantile([fr for _, _, fr in feats], 0.80))
        for sym, tr, fr in feats:
            rows.append({
                "date_idx": i, "regime": regime,
                "feat": None,                       # filled below
                "fwd": fr,
                "y_up": 1.0 if fr > 0 else 0.0,          # target A
                "y_better_med": 1.0 if fr > med else 0.0,# target B1: beat median
                "y_topq": 1.0 if fr >= q80 else 0.0,     # target B2: top-quintile
                "sym": sym,
            })

# Attach full lookback feature vector [L7,L14,L30,L60] per (date, coin).
# We need, per (date_idx, sym), the returns at L7..L60. Compute from price series.
def feat_vector(date_ts, sym):
    v = []
    for L in LOOKBACKS:
        # find date L back from date_ts
        idx = date_index.get(date_ts)
        if idx is None or idx - L < 0:
            return None
        t0 = btc_dates[idx - L]
        p0 = price_at(sym, t0); p1 = price_at(sym, date_ts)
        if p0 and p1 and p0 > 0:
            v.append(p1/p0 - 1)
        else:
            return None
    return v

date_index = {t: i for i, t in enumerate(btc_dates)}
full = []
for r in rows:
    fv = feat_vector(btc_dates[r["date_idx"]], r["sym"])
    if fv:
        r["feat"] = fv
        full.append(r)

print(f"\nTotal labeled rows: {len(full)}")

# --- Logistic regression (numpy, L2, gradient descent) ---
def train_logistic(X, y, l2=1e-3, steps=400, lr=0.5):
    n, d = X.shape
    Xb = np.hstack([np.ones((n, 1)), X])
    w = np.zeros(d + 1)
    for _ in range(steps):
        z = Xb @ w
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -500, 500)))
        grad = Xb.T @ (p - y) / n + l2 * np.concatenate([[0], w[1:]])
        w -= lr * grad
    return w

def predict_proba(X, w):
    Xb = np.hstack([np.ones((X.shape[0], 1)), X])
    z = Xb @ w
    return 1.0 / (1.0 + np.exp(-np.clip(z, -500, 500)))

def auc(y_true, scores):
    """Mann-Whitney U based AUC, pure python fallback for small sets."""
    pos = [s for y, s in zip(y_true, scores) if y == 1]
    neg = [s for y, s in zip(y_true, scores) if y == 0]
    if not pos or not neg:
        return None
    n1, n2 = len(pos), len(neg)
    if n1 * n2 == 0:
        return None
    # U statistic
    pos_sorted = sorted(pos); neg_sorted = sorted(neg)
    U = 0.0; j = 0
    for pv in pos_sorted:
        while j < n2 and neg_sorted[j] < pv:
            j += 1
        U += j
        # ties: count equal negatives
        k = j
        while k < n2 and neg_sorted[k] == pv:
            k += 1
        U += (k - j) / 2.0
    return U / (n1 * n2)

# --- Chronological split: train on first 70% of DATES, test on last 30% ---
dates = sorted({r["date_idx"] for r in full})
split_pt = dates[int(len(dates)*0.70)]
train = [r for r in full if r["date_idx"] < split_pt]
test  = [r for r in full if r["date_idx"] >= split_pt]
print(f"Chronological split: train {len(train)} rows (dates < {_fmt(btc_dates[split_pt])}), "
      f"test {len(test)} rows")

def run_target(name, key):
    Xtr = np.array([r["feat"] for r in train], dtype=float)
    ytr = np.array([r[key] for r in train], dtype=float)
    Xte = np.array([r["feat"] for r in test], dtype=float)
    yte = np.array([r[key] for r in test], dtype=float)
    # standardize by train stats
    mu = Xtr.mean(0); sd = Xtr.std(0) + 1e-9
    Xtr_s = (Xtr - mu)/sd; Xte_s = (Xte - mu)/sd
    w = train_logistic(Xtr_s, ytr)
    pte = predict_proba(Xte_s, w)
    a = auc(yte.tolist(), pte.tolist())
    base = yte.mean()
    print(f"\n  [{name}]  base-rate(y=1)={base:.3f}  out-of-sample AUC={a:.3f}  "
          f"({'EDGE > 0.5' if a and a > 0.52 else '~coin-flip / no edge'})")
    # per-regime AUC on test
    for reg in ["BULL", "SIDEWAYS", "BEAR"]:
        m = [(r[key], p) for r, p in zip(test, pte) if r["regime"] == reg]
        if len(m) > 20:
            yy = [x[0] for x in m]; pp = [x[1] for x in m]
            ra = auc(yy, pp)
            print(f"      {reg:9} n={len(m):6} AUC={ra if ra is None else round(ra,3)}")
    return a

print("\n=== PROBABILISTIC MODEL ON THE SAME MOMENTUM FEATURES ===\n")
print("Target A: P(forward return > 0)")
run_target("time-series up/down", "y_up")
print("\nTarget B1: P(coin beats median forward return that day)")
run_target("cross-sec beat-median", "y_better_med")
print("\nTarget B2: P(coin in top-quintile forward return that day)")
run_target("cross-sec top-quintile", "y_topq")

print("""
INTERPRETATION:
- If all out-of-sample AUC ~ 0.5, the probability framing changes NOTHING:
  it is an output representation of features that carry no forward info.
  Calibrated probabilities would just honestly report ~50% everywhere.
- A probability model is only worth building if the FEATURES carry signal.
  The one untested-feature path remains fundamental/VaF (needs point-in-time
  history) — NOT re-encoding the same price/volume momentum.
""")

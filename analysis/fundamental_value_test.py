"""Fundamental / value-accrual PREDICTIVE test — the last open path.

After momentum, technicals, and their probability re-encoding were all
falsified cross-era (see VALIDATION.md), the only untested feature family
is FUNDAMENTAL / value-accrual: does a coin's on-chain economics
(TVL growth, fee/revenue yield, value accrual) predict its forward
return? This is economically DIFFERENT information from price history
— value investing works by mean-reversion to fundamentals, not by price
momentum — so it deserves its own honest cross-era test.

DATA
  Features: read from history.db `snapshots` payload, per (date, coin),
  from `fundamental_detail` / `fundamental_raw` / `vaf` / `mcap`
  (persisted point-in-time by collect.py since 2026-07-13). These are
  POINT-IN-TIME: the exact values the collector saw that day, so no
  lookahead bias.
  Labels: forward 7d return computed from Binance Vision 1d klines
  (cached in /tmp/bv_monthly_cache, same canonical source as
  momentum_crossera_test.py).

METHOD (identical discipline to the momentum test)
  Features per coin: tvl_growth_30d, fee_accel_7v30, value_accrual_ratio,
  revenue_yield_ann (if present), price_to_fees (value multiple — model
  may learn either sign), vaf score + otf.
  Logistic regression (L2, numpy), chronological split (train on first
  70% of DATES, evaluate on last 30%), out-of-sample AUC.
  Targets: cross-sectional "beats median forward return" and "top
  quintile" — i.e. "which coin wins", the question momentum failed.

HONESTY GUARD
  Requires >= MIN_DAYS of history AND >= 2 regimes with >= MIN_SAMPLES
  each, else it reports INSUFFICIENT DATA instead of fabricating a
  result from too little data. Run it periodically; it turns meaningful
  the day enough point-in-time history has accumulated.

Usage:  .venv/bin/python analysis/fundamental_value_test.py
"""
import os, sqlite3, json, datetime
from collections import defaultdict
import numpy as np

DB = os.environ.get("HISTORY_DB", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "history.db"))
CACHE = "/tmp/bv_monthly_cache"
HORIZON = 7
MIN_DAYS = 60
MIN_SAMPLES = 30

# Binance symbol -> price source. Reuse the same 14-coin universe so the
# two tests are directly comparable; extend as fundamental coverage grows.
COINS = ["AAVEUSDT", "UNIUSDT", "PENDLEUSDT", "LDOUSDT", "CRVUSDT",
         "COMPUSDT", "GMXUSDT", "DYDXUSDT", "JUPUSDT", "RAYUSDT",
         "CAKEUSDT", "SNXUSDT", "ENAUSDT", "MKRUSDT", "LINKUSDT"]


def _normalize_ts(ts):
    if ts > 1e14:
        return ts / 1000
    if ts > 1e11:
        return ts
    return ts * 1000


def _fmt(ts):
    return datetime.datetime.fromtimestamp(ts/1000, datetime.timezone.utc).date().isoformat()


# ── Load point-in-time fundamental features from history.db ──
def load_fundamental_panel():
    """[(date, symbol, features_dict)] for every snapshot with fundamentals."""
    c = sqlite3.connect(DB)
    rows = c.execute(
        "SELECT date, symbol, payload FROM snapshots "
        "WHERE payload LIKE '%\"fundamental_detail\"%' ORDER BY date ASC").fetchall()
    c.close()
    panel = []
    for date, symbol, payload in rows:
        try:
            p = json.loads(payload)
        except ValueError:
            continue
        fd = p.get("fundamental_detail") or {}
        raw = p.get("fundamental_raw") or {}
        vaf = p.get("vaf") or {}
        feats = {}
        if fd.get("tvl_growth_30d") is not None:
            feats["tvl_growth_30d"] = float(fd["tvl_growth_30d"])
        if fd.get("fee_accel_7v30") is not None:
            feats["fee_accel_7v30"] = float(fd["fee_accel_7v30"])
        if fd.get("value_accrual_ratio") is not None:
            feats["value_accrual_ratio"] = float(fd["value_accrual_ratio"])
        if fd.get("revenue_yield_ann") is not None:
            feats["revenue_yield_ann"] = float(fd["revenue_yield_ann"])
        if fd.get("price_to_fees") is not None:
            feats["price_to_fees"] = float(fd["price_to_fees"])
        if vaf.get("vaf") is not None:
            feats["vaf"] = float(vaf["vaf"])
        if vaf.get("otf") is not None:
            feats["otf"] = float(vaf["otf"])
        # raw absolute fees -> log scale (right-skewed)
        if raw.get("fees_30d"):
            feats["log_fees_30d"] = np.log(float(raw["fees_30d"]))
        if not feats:
            continue
        panel.append({"date": date, "symbol": symbol, "feats": feats})
    return panel


# ── Build price matrix from Binance Vision cache ──
def load_prices():
    series = {}
    for sym in COINS:
        by_ts = {}
        prefix = f"{sym}-"
        for fn in os.listdir(CACHE):
            if fn.startswith(prefix) and fn.endswith(".csv"):
                with open(os.path.join(CACHE, fn)) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        t, close = line.split(",")
                        by_ts[_normalize_ts(int(t))] = float(close)
        if by_ts:
            series[sym] = sorted(by_ts.items())
    return series


def build_test_set(panel, prices):
    """Merge features with forward returns; return list of records."""
    by_sym = {sym: dict(series) for sym, series in prices.items()}
    # date -> (date_ts) map from a reference symbol (BTC grid)
    ref = "AAVEUSDT"
    if ref not in by_sym:
        ref = next(iter(by_sym))
    ref_dates = [t for t, _ in prices[ref]]
    # helper: price on date string
    def price_on(sym, date):
        d = by_sym.get(sym)
        if not d:
            return None
        # find the price at/after date
        target = datetime.datetime.strptime(date, "%Y-%m-%d").timestamp() * 1000
        # cache-adjacent: klines are daily; find nearest ts >= target
        keys = [t for t in d if t >= target]
        return d[min(keys)] if keys else None
    recs = []
    for r in panel:
        p0 = price_on(r["symbol"], r["date"])
        if p0 is None:
            continue
        # forward return: price HORIZON days later
        t0 = datetime.datetime.strptime(r["date"], "%Y-%m-%d").timestamp() * 1000
        future = [t for t in by_sym.get(r["symbol"], {}) if t >= t0 + HORIZON*86400*1000]
        if not future:
            continue
        pf = by_sym[r["symbol"]][min(future)]
        if p0 > 0 and pf > 0:
            recs.append({"date": r["date"], "symbol": r["symbol"],
                         "feats": r["feats"], "fwd": pf/p0 - 1})
    return recs


def spearman_auc(y_true, scores):
    pos = [s for y, s in zip(y_true, scores) if y == 1]
    neg = [s for y, s in zip(y_true, scores) if y == 0]
    if not pos or not neg:
        return None
    n1, n2 = len(pos), len(neg)
    pos_sorted = sorted(pos); neg_sorted = sorted(neg)
    U = 0.0; j = 0
    for pv in pos_sorted:
        while j < n2 and neg_sorted[j] < pv:
            j += 1
        U += j
        k = j
        while k < n2 and neg_sorted[k] == pv:
            k += 1
        U += (k - j) / 2.0
    return U / (n1 * n2)


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


def main():
    print(f"DB: {DB}")
    panel = load_fundamental_panel()
    days = len({r['date'] for r in panel})
    print(f"Fundamental snapshots: {len(panel)} rows across {days} days, "
          f"{len({r['symbol'] for r in panel})} symbols")
    print(f"  date range: {min(r['date'] for r in panel)} .. {max(r['date'] for r in panel)}")

    if days < MIN_DAYS:
        print(f"\nINSUFFICIENT DATA: only {days} days of point-in-time fundamentals "
              f"(need >= {MIN_DAYS}).\n"
              "This is the honest expected state until the collector has run "
              "longer. Re-run periodically; the framework is ready.")
        return

    prices = load_prices()
    recs = build_test_set(panel, prices)
    print(f"Mergeable with forward returns: {len(recs)} rows")
    if len(recs) < 100:
        print("\nINSUFFICIENT DATA after merge — re-run later.")
        return

    # regimes from date (approximate via 30d BTC trend is complex; use
    # stored regime from history.db macro join by date)
    c = sqlite3.connect(DB)
    mac = dict(c.execute("SELECT date, payload FROM macro").fetchall())
    c.close()
    def regime_for(date):
        try:
            return (json.loads(mac.get(date, "{}")).get("regime") or {}).get("state")
        except ValueError:
            return None
    for r in recs:
        r["regime"] = regime_for(r["date"])

    # regime diversity check
    from collections import Counter
    rc = Counter(r["regime"] for r in recs if r["regime"])
    if sum(1 for v in rc.values() if v >= MIN_SAMPLES) < 2:
        print(f"\nINSUFFICIENT REGIME DIVERSITY: {dict(rc)} "
              f"(need >= 2 regimes with >= {MIN_SAMPLES} samples each).")
        return

    # feature alignment
    feat_names = sorted({k for r in recs for k in r["feats"]})
    print(f"\nFeatures ({len(feat_names)}): {feat_names}")
    X = np.array([[r['feats'].get(k, 0.0) for k in feat_names] for r in recs], dtype=float)
    dates = sorted({r['date'] for r in recs})
    split_pt = dates[int(len(dates)*0.70)]
    tr_idx = [i for i, r in enumerate(recs) if r['date'] < split_pt]
    te_idx = [i for i, r in enumerate(recs) if r['date'] >= split_pt]
    print(f"Chronological split: train {len(tr_idx)} (dates < {split_pt}), test {len(te_idx)}")

    mu = X[tr_idx].mean(0); sd = X[tr_idx].std(0) + 1e-9
    Xtr = (X[tr_idx] - mu)/sd; Xte = (X[te_idx] - mu)/sd

    for name, key in [("beats median (cross-sec)", "y_med"),
                      ("top-quintile (cross-sec)", "y_topq")]:
        # compute target within each date cross-section
        ytr = []; yte = []
        def target(recs_idx):
            out = []
            by_date = defaultdict(list)
            for i in recs_idx:
                by_date[recs[i]['date']].append(i)
            for d, idxs in by_date.items():
                fwd = [recs[i]['fwd'] for i in idxs]
                med = float(np.median(fwd)); q80 = float(np.quantile(fwd, 0.80))
                for i in idxs:
                    fr = recs[i]['fwd']
                    out.append((i, 1.0 if (fr > med if key == "y_med" else fr >= q80) else 0.0))
            out.sort()
            return [v for _, v in out]
        ytr = target(tr_idx); yte = target(te_idx)
        ytr = np.array(ytr); yte = np.array(yte)
        if ytr.mean() == 0 or ytr.mean() == 1 or yte.mean() == 0 or yte.mean() == 1:
            print(f"\n  [{name}] degenerate target (base rate {yte.mean():.3f}) — skip")
            continue
        w = train_logistic(Xtr, ytr)
        pte = predict_proba(Xte, w)
        a = spearman_auc(yte.tolist(), pte.tolist())
        print(f"\n  [{name}]  OOS AUC={a if a is None else round(a,3)}  "
              f"base-rate={yte.mean():.3f}  "
              f"({'EDGE > 0.5' if a and a > 0.52 else '~coin-flip / no edge'})")
        for reg in ["BULL", "SIDEWAYS", "BEAR"]:
            m = [(recs[te_idx[j]]['regime'] == reg, yte[j], pte[j])
                 for j in range(len(te_idx))]
            m = [(y, p) for rfl, y, p in m if rfl]
            if len(m) > 20:
                ra = spearman_auc([x[0] for x in m], [x[1] for x in m])
                print(f"      {reg:9} n={len(m):5} AUC={ra if ra is None else round(ra,3)}")

    print("\nINTERPRETATION: value factors may behave like momentum (no edge) OR "
          "provide a real edge via fundamental mean-reversion. The number above is "
          "the honest cross-era answer. If ~0.5 cross-era, fundamental selection "
          "is also dead and the terminal is honestly intelligence-only.")


if __name__ == "__main__":
    main()

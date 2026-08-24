"""Volume Flow Layer test — era/regime-split validation for the two signals
proposed in a.docx (Early Volume Accumulation & QI Flow Rotation).

Methodology:
  - Reconstructs multi-year (2017-2026) daily series for long-history coins
    from Binance Vision monthly klines (same data source & cache as
    momentum_crossera_test.py).
  - Computes the ACTUAL engine volume metrics via altcoin.analyzer's
    _compute_volume_metrics (vol_ratio = vol_24h/avg_7d spike detector,
    vol_trend = avg_3d/avg_7d acceleration) — faithful to the production
    function, not a reimplementation.
  - "QI active" is operationalised per-coin as a strong positive 30d trend
    (trail_30d > +15%), matching the doc's "trend sudah masuk QI".
  - Two signals:
      1. EARLY ACCUMULATION (QI NOT active): volume spike + price NOT yet
         repriced.  Score = vol_ratio - 2*prox_30d_high  (∝ Volume
         Expansion − Price Response). Binary trigger: vol_ratio >= 1.2
         (valid) / >= 1.3 (strong) AND prox_30d_high < 0.5.
      2. QI FLOW ROTATION (QI active): volume warming + persistence.
         Score = vol_trend. Binary trigger: vol_trend >= 1.10 (warming)
         AND vol_ratio >= 1.0 (sustained, not a one-day fade).
  - Cross-sectional IC (Spearman of score vs 7d forward return) pooled PER
    ERA (BULL/SIDEWAYS/BEAR by BTC 30d trend), plus hit-rate of the binary
    triggers vs baseline.

ERA CAVEAT (honest): each era is a different market condition — a signal can
legitimately differ per era. We do NOT reject on a single era flip; we report
per-era IC sign/consistency AND the pooled effect, and only call an edge real
if it is positive (IC>0) in MULTIPLE eras or clearly pooled-positive.

Diagnostic only. Does NOT modify scoring.
"""
import urllib.request, zipfile, io, csv, os, time, datetime
from collections import defaultdict
from statistics import mean, median

from altcoin.analyzer import _compute_volume_metrics  # ACTUAL engine function

BASE = "https://data.binance.vision/data/spot/monthly/klines"
COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT", "AAVEUSDT", "UNIUSDT",
         "CRVUSDT", "SNXUSDT", "COMPUSDT", "MKRUSDT", "DOGEUSDT", "RAYUSDT",
         "DYDXUSDT", "LDOUSDT"]
HORIZON = 7
CACHE = "/tmp/bv_monthly_cache"
os.makedirs(CACHE, exist_ok=True)


def _normalize_ts(ts):
    if ts > 1e14:          # microseconds (newer files)
        return ts / 1000
    if ts > 1e11:          # milliseconds (normal)
        return ts
    return ts * 1000       # seconds


def fetch_month(sym, year, month):
    fn = f"{CACHE}/{sym}-{year}-{month:02d}.csv"
    if os.path.exists(fn):
        return _read_csv(fn)
    url = f"{BASE}/{sym}/1d/{sym}-1d-{year:04d}-{month:02d}.zip"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/7.8"})
        data = urllib.request.urlopen(req, timeout=40).read()
    except Exception:
        return None
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        raw = z.read(z.namelist()[0]).decode()
    rows = []
    for line in raw.strip().splitlines():
        p = line.split(",")
        # kline: [0]ot [1]open [2]high [3]low [4]close [5]vol [6]ct ...
        try:
            t = _normalize_ts(int(p[0])); close = float(p[4]); vol = float(p[5])
        except (ValueError, IndexError):
            continue
        rows.append((int(t), close, vol))
    with open(fn, "w") as f:
        for t, c, v in rows:
            f.write(f"{t},{c},{v}\n")
    return rows


def _read_csv(fn):
    rows = []
    with open(fn) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            t, c, v = line.split(",")
            rows.append((_normalize_ts(int(t)), float(c), float(v)))
    return rows


def _fmt(ts):
    return datetime.datetime.fromtimestamp(ts / 1000, datetime.timezone.utc).date().isoformat()


# ---- Build daily series per symbol (close, volume) merged & ordered ----
series = {}
for sym in COINS:
    by_ts = {}
    for year in range(2017, 2027):
        for month in range(1, 13):
            rows = fetch_month(sym, year, month)
            if rows:
                for t, c, v in rows:
                    by_ts[t] = (c, v)
            time.sleep(0.02)
    series[sym] = sorted(by_ts.items())
    print(f"  {sym}: {len(series[sym])} days  {_fmt(series[sym][0][0])}..{_fmt(series[sym][-1][0])}", flush=True)

btc_dates = [t for t, _ in series["BTCUSDT"]]
date_index = {t: i for i, t in enumerate(btc_dates)}
prices = {s: dict((t, c) for t, (c, _) in series[s]) for s in series}
volumes = {s: dict((t, v) for t, (_, v) in series[s]) for s in series}


def spearman(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    def ranks(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0.0] * len(vals); i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    n = len(pairs); mx = sum(rx) / n; my = sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx); vy = sum((b - my) ** 2 for b in ry)
    if vx <= 1e-12 or vy <= 1e-12:
        return None
    return cov / (vx ** 0.5 * vy ** 0.5)


def btc_ret30(idx):
    i0 = max(0, idx - 30)
    p0 = prices["BTCUSDT"].get(btc_dates[i0]); p1 = prices["BTCUSDT"].get(btc_dates[idx])
    if p0 and p1 and p0 > 0:
        return p1 / p0 - 1
    return None


def vol_metrics_at(sym, idx):
    """Feed the ACTUAL engine _compute_volume_metrics a trailing slice of
    quote volumes (oldest-first, >= 8 closed candles) ending at `idx`."""
    dates = btc_dates[max(0, idx - 40): idx + 1]
    qv = [volumes[sym].get(d) for d in dates]
    if any(v is None for v in qv) or len(qv) < 9:
        return None
    return _compute_volume_metrics([v for v in qv])


def trail_ret(sym, idx, lookback=30):
    i0 = max(0, idx - lookback)
    p0 = prices[sym].get(btc_dates[i0]); p1 = prices[sym].get(btc_dates[idx])
    if p0 and p1 and p0 > 0:
        return p1 / p0 - 1
    return None


def prox_30d_high(sym, idx):
    """Position in trailing 30d range: 1.0 = at 30d high, 0 = at 30d low."""
    dates = btc_dates[max(0, idx - 30): idx + 1]
    closes = [prices[sym].get(d) for d in dates]
    if any(c is None for c in closes):
        return None
    hi, lo = max(closes), min(closes)
    cur = closes[-1]
    if hi == lo:
        return 0.5
    return (cur - lo) / (hi - lo)


# ---- Cross-era cross-sectional evaluation ----
# For each date, compute each signal's score across coins and the IC vs 7d fwd return.
sig_ic = defaultdict(list)      # (era, signal) -> list of per-date IC
trig = defaultdict(list)        # (era, signal) -> rows of {fwd, hit}
for i in range(40, len(btc_dates)):
    t = btc_dates[i]
    if i + HORIZON >= len(btc_dates):
        break
    t_fwd = btc_dates[i + HORIZON]
    r30 = btc_ret30(i)
    if r30 is None:
        continue
    era = "BULL" if r30 > 0.15 else ("BEAR" if r30 < -0.15 else "SIDEWAYS")
    rows = []
    for sym in COINS:
        if sym == "BTCUSDT":
            continue
        p1 = prices[sym].get(t); pf = prices[sym].get(t_fwd)
        if not p1 or not pf or p1 <= 0:
            continue
        fwd = pf / p1 - 1
        vm = vol_metrics_at(sym, i)
        if not vm:
            continue
        vr = vm["vol_ratio"]; vt = vm["vol_trend"]
        if vr is None or vt is None:
            continue
        qi = (trail_ret(sym, i) or 0) > 0.15
        prox = prox_30d_high(sym, i)
        if prox is None:
            continue
        if not qi:  # EARLY ACCUMULATION (pre-QI)
            score = vr - 2.0 * prox          # ∝ Volume Expansion − Price Response
            trig_bin = (vr >= 1.2) and (prox < 0.5)
            trig_strong = (vr >= 1.3) and (prox < 0.5)
            rows.append(("early", score, fwd, trig_bin, trig_strong))
        else:       # QI FLOW ROTATION (QI active)
            score = vt                           # volume warming
            trig_bin = (vt >= 1.10) and (vr >= 1.0)
            rows.append(("qiflow", score, fwd, trig_bin, trig_bin))
    for sig in ("early", "qiflow"):
        sub = [r for r in rows if r[0] == sig]
        if len(sub) < 5:
            continue
        ic = spearman([r[1] for r in sub], [r[2] for r in sub])
        if ic is not None:
            sig_ic[(era, sig)].append(ic)
        for _, score, fwd, tb, ts in sub:
            trig[sig].append({"era": era, "fwd": fwd, "b": tb, "s": ts})

print("\n=== VOLUME FLOW LAYER — CROSS-ERA CROSS-SECTIONAL IC ===")
print(f"{'era':10} {'signal':7} {'dates':>6} {'meanIC':>8} {'IC>0':>7} {'medIC':>7}")
print("-" * 50)
for era in ["BULL", "SIDEWAYS", "BEAR"]:
    for sig, name in [("early", "early-acc"), ("qiflow", "qi-flow")]:
        ics = sig_ic.get((era, sig), [])
        if not ics:
            print(f"{era:10} {name:7} {'0':>6} {'n/a':>8} {'n/a':>7} {'n/a':>7}")
            continue
        pos = sum(1 for x in ics if x > 0) / len(ics)
        print(f"{era:10} {name:7} {len(ics):>6} {mean(ics):>8.3f} {pos:>7.0%} {median(ics):>7.3f}")

print("\n=== BINARY TRIGGER HIT-RATE (fwd return > 0) vs BASELINE, per era ===")
print(f"{'era':10} {'signal':10} {'trig(n)':>9} {'trigHit':>9} {'base(n)':>8} {'baseHit':>8} {'gap':>7}")
print("-" * 64)
for era in ["BULL", "SIDEWAYS", "BEAR"]:
    # baseline = all non-trigger rows (both signals) in this era
    base_rows = [r for sig in ("early", "qiflow") for r in trig[sig] if r["era"] == era and not r["b"]]
    base_n = len(base_rows)
    base_hit = (sum(1 for r in base_rows if r["fwd"] > 0) / base_n) if base_n else None
    for sig, name, use_strong in [("early", "early-acc", False), ("qiflow", "qi-flow", False),
                                  ("early", "early-strong", True)]:
        rr = [r for r in trig[sig] if r["era"] == era and (r["s"] if use_strong else r["b"])]
        n = len(rr); hit = (sum(1 for r in rr if r["fwd"] > 0) / n) if n else None
        gap = (hit - base_hit) if (hit is not None and base_hit is not None) else None
        gap_s = f"{gap:+.3f}" if gap is not None else "n/a"
        print(f"{era:10} {name:10} {n:>9} {str(hit and round(hit,3)):>9} {str(base_n):>8} "
              f"{str(base_hit and round(base_hit,3)):>8} {gap_s:>7}")

print("""
INTERPRETATION (ERA-AWARE):
- Each era is a different market condition; a signal may legitimately differ
  per era. We DO NOT reject on a single era flip.
- A real edge needs: mean IC clearly > 0 AND IC>0 fraction well above 0.55 in
  MULTIPLE eras, AND a positive trigger hit-rate gap vs baseline in those eras.
- If IC is flat/negative everywhere, the volume-flow layer adds no
  cross-sectional edge independent of era. If it is positive ONLY in BULL,
  that is a regime-conditional result, not a universal edge.
""")

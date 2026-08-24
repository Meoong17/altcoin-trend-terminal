"""Volume Flow Layer test — BTC-DOMINANCE-split (altcoin regime axis).

Correction incorporated: for ALTCOINS the influential regime variable is BTC
dominance (btcd), NOT macro. Altcoins are sensitive to btcd dynamics (whether
BTC is absorbing flows / rising dominance, or de-risking into alt season /
falling dominance). So the cross-sectional era split below is conditioned on
a btcd proxy, not on macro or a plain BTC-30d bull/bear label.

BTC-dominance proxy (price-based, from the same Binance Vision series):
    btcd(date) = btc_ret30(date) - median_alt_ret30(date)
    positive => BTC outperforming the alt basket => dominance RISING
    negative => alts outperforming => dominance FALLING (alt season)

Eras (3-way):
    ALT_SEASON  btcd < -0.10   (dominance falling, alts leading)
    BTC_UP      btcd > +0.10   (dominance rising, BTC leading)
    FLAT        otherwise

Signals (unchanged operationalisation, using the ACTUAL engine function):
    1. EARLY ACCUMULATION (per-coin QI NOT active): vol_ratio spike + price
       not yet repriced.  Score = vol_ratio - 2*prox_30d_high.
       Binary: vol_ratio >= 1.2 AND prox_30d_high < 0.5 (strong: >=1.3).
    2. QI FLOW ROTATION (per-coin QI active): volume warming + persistence.
       Score = vol_trend.  Binary: vol_trend >= 1.10 AND vol_ratio >= 1.0.

ERA-AWARE VERDICT RULE: a real edge needs mean IC > 0 AND IC>0 fraction well
above 0.55 in MULTIPLE btcd eras, plus a positive trigger hit-rate gap vs
baseline. Single-era effects are regime-conditional, not universal. A factor
flip across eras is expected (each era differs) and is NOT by itself a reject;
what matters is whether it is positive where it matters and sign-consistent
across the alts-relevant eras.

Diagnostic only. Does NOT modify scoring.
"""
import os, time, datetime, statistics
from collections import defaultdict
from statistics import mean, median
import urllib.request, zipfile, io

from altcoin.analyzer import _compute_volume_metrics  # ACTUAL engine function

BASE = "https://data.binance.vision/data/spot/monthly/klines"
COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT", "AAVEUSDT", "UNIUSDT",
         "CRVUSDT", "SNXUSDT", "COMPUSDT", "MKRUSDT", "DOGEUSDT", "RAYUSDT",
         "DYDXUSDT", "LDOUSDT"]
HORIZON = 7
CACHE = "/tmp/bv_monthly_cache"
os.makedirs(CACHE, exist_ok=True)
ALTS = [c for c in COINS if c != "BTCUSDT"]


def _normalize_ts(ts):
    if ts > 1e14:
        return ts / 1000
    if ts > 1e11:
        return ts
    return ts * 1000


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
        try:
            t = _normalize_ts(int(p[0])); c = float(p[4]); v = float(p[5])
        except (ValueError, IndexError):
            continue
        rows.append((int(t), c, v))
    with open(fn, "w") as f:
        for t, c, v in rows:
            f.write(f"{t},{c},{v}\n")
    return rows


def _fmt(ts):
    return datetime.datetime.fromtimestamp(ts / 1000, datetime.timezone.utc).date().isoformat()


# Build series (cache-backed; fills any months the background run missed)
series = {}
for sym in COINS:
    by_ts = {}
    for year in range(2017, 2027):
        for month in range(1, 13):
            rows = fetch_month(sym, year, month)
            if rows:
                for t, c, v in rows:
                    by_ts[t] = (c, v)
    series[sym] = sorted(by_ts.items())
    print(f"  {sym}: {len(series[sym])} days  {_fmt(series[sym][0][0])}..{_fmt(series[sym][-1][0])}", flush=True)

btc_dates = [t for t, _ in series["BTCUSDT"]]
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


def ret30(sym, idx):
    i0 = max(0, idx - 30)
    p0 = prices[sym].get(btc_dates[i0]); p1 = prices[sym].get(btc_dates[idx])
    if p0 and p1 and p0 > 0:
        return p1 / p0 - 1
    return None


def btcd_proxy(idx):
    """btc_ret30 minus median alt ret30. + => dominance rising, - => alt season."""
    b = ret30("BTCUSDT", idx)
    if b is None:
        return None
    a = [r for r in (ret30(s, idx) for s in ALTS) if r is not None]
    if not a:
        return None
    return b - median(a)


def vol_metrics_at(sym, idx):
    dates = btc_dates[max(0, idx - 40): idx + 1]
    qv = [volumes[sym].get(d) for d in dates]
    if any(v is None for v in qv) or len(qv) < 9:
        return None
    return _compute_volume_metrics([v for v in qv])


def prox_30d_high(sym, idx):
    dates = btc_dates[max(0, idx - 30): idx + 1]
    closes = [prices[sym].get(d) for d in dates]
    if any(c is None for c in closes):
        return None
    hi, lo = max(closes), min(closes)
    cur = closes[-1]
    if hi == lo:
        return 0.5
    return (cur - lo) / (hi - lo)


sig_ic = defaultdict(list)
trig = defaultdict(list)
for i in range(40, len(btc_dates)):
    t = btc_dates[i]
    if i + HORIZON >= len(btc_dates):
        break
    t_fwd = btc_dates[i + HORIZON]
    bp = btcd_proxy(i)
    if bp is None:
        continue
    era = "ALT_SEASON" if bp < -0.10 else ("BTC_UP" if bp > 0.10 else "FLAT")
    rows = []
    for sym in ALTS:
        p1 = prices[sym].get(t); pf = prices[sym].get(t_fwd)
        if not p1 or not pf or p1 <= 0:
            continue
        fwd = pf / p1 - 1
        vm = vol_metrics_at(sym, i)
        if not vm or vm["vol_ratio"] is None or vm["vol_trend"] is None:
            continue
        vr, vt = vm["vol_ratio"], vm["vol_trend"]
        qi = (ret30(sym, i) or 0) > 0.15
        prox = prox_30d_high(sym, i)
        if prox is None:
            continue
        if not qi:
            rows.append(("early", vr - 2.0 * prox, fwd, (vr >= 1.2 and prox < 0.5),
                         (vr >= 1.3 and prox < 0.5)))
        else:
            rows.append(("qiflow", vt, fwd, (vt >= 1.10 and vr >= 1.0), (vt >= 1.10 and vr >= 1.0)))
    for sig in ("early", "qiflow"):
        sub = [r for r in rows if r[0] == sig]
        if len(sub) < 5:
            continue
        ic = spearman([r[1] for r in sub], [r[2] for r in sub])
        if ic is not None:
            sig_ic[(era, sig)].append(ic)
        for _, sc, fwd, b, s in sub:
            trig[sig].append({"era": era, "fwd": fwd, "b": b, "s": s})

print("\n=== VOLUME FLOW LAYER — CROSS-ERA CROSS-SECTIONAL IC (era = BTC DOMINANCE) ===")
print(f"{'btcd era':11} {'signal':7} {'dates':>6} {'meanIC':>8} {'IC>0':>7} {'medIC':>7}")
print("-" * 52)
for era in ["ALT_SEASON", "FLAT", "BTC_UP"]:
    for sig, name in [("early", "early-acc"), ("qiflow", "qi-flow")]:
        ics = sig_ic.get((era, sig), [])
        if not ics:
            print(f"{era:11} {name:7} {'0':>6} {'n/a':>8} {'n/a':>7} {'n/a':>7}")
            continue
        pos = sum(1 for x in ics if x > 0) / len(ics)
        print(f"{era:11} {name:7} {len(ics):>6} {mean(ics):>8.3f} {pos:>7.0%} {median(ics):>7.3f}")

print("\n=== BINARY TRIGGER HIT-RATE (fwd>0) vs BASELINE, per btcd era ===")
print(f"{'btcd era':11} {'signal':10} {'trig(n)':>9} {'trigHit':>9} {'base(n)':>8} {'baseHit':>8} {'gap':>7}")
print("-" * 66)
for era in ["ALT_SEASON", "FLAT", "BTC_UP"]:
    base_rows = [r for sig in ("early", "qiflow") for r in trig[sig] if r["era"] == era and not r["b"]]
    base_n = len(base_rows)
    base_hit = (sum(1 for r in base_rows if r["fwd"] > 0) / base_n) if base_n else None
    for sig, name, use_strong in [("early", "early-acc", False), ("qiflow", "qi-flow", False),
                                  ("early", "early-strong", True)]:
        rr = [r for r in trig[sig] if r["era"] == era and (r["s"] if use_strong else r["b"])]
        n = len(rr); hit = (sum(1 for r in rr if r["fwd"] > 0) / n) if n else None
        gap = (hit - base_hit) if (hit is not None and base_hit is not None) else None
        gap_s = f"{gap:+.3f}" if gap is not None else "n/a"
        print(f"{era:11} {name:10} {n:>9} {str(hit and round(hit,3)):>9} {str(base_n):>8} "
              f"{str(base_hit and round(base_hit,3)):>8} {gap_s:>7}")

print("""
INTERPRETATION (BTC-DOMINANCE-AWARE):
- ALT_SEASON = dominance falling, alts leading -> where alt volume signals
  SHOULD matter most. A volume-flow edge that shows here is regime-relevant.
- BTC_UP = dominance rising, BTC absorbing flows -> alt signals expected weak.
- Each btcd era is a different flow regime; per-era differences are expected.
  Real edge = positive IC in multiple eras (especially ALT_SEASON) + positive
  hit-rate gap vs baseline. Flat/negative everywhere = no cross-sectional edge.
""")

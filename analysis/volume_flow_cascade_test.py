"""Volume Flow CASCADE test — "where does money move before price realizes it?"

Thesis being validated (as clarified):
  The edge is LEADING flow: money starts moving into a coin before the
  majority of price has repriced it. But this token-level flow signal is
  CONDITIONAL — it only fires after a context gate supports it:
      gate = macro + BTC + ETH(majors) + rotation all supportive.
  ETH is a MAJOR (co-leading with BTC), NOT a member of the alt basket.

This harness tests the testable core of that cascade with the data we have:
  - Context gate (majors + rotation) from the same price series.
  - Token-level VOLUME + PRICE-RESPONSE signal (the ACTUAL engine function).
  - Does the token signal predict forward returns BETTER inside the
    favorable context than outside? And does "volume up while price is
    still low" (early accumulation) beat "volume up after price already ran"
    (breakout chasing) — the money-before-price question?

HONEST DATA LIMITS (documented, not hidden):
  - From Binance Vision klines we CAN compute: volume (vol_ratio, vol_trend),
    price response (prox_30d_high). 
  - We CANNOT compute from this source: net buy, smart-wallet activity,
    exchange flows, liquidity growth. Those order-flow components of the
    thesis are marked NOT-YET-VALIDATABLE here.
  - Macro is treated as a background filter that this price-only source
    cannot compute; the gate below uses majors (BTC+ETH) + rotation only.

ERA-AWARE RULE (each regime/context differs): we report the signal's IC and
hit-rate per context state and only call an edge real if it is positive and
sign-consistent across the relevant favorable states (esp. ALT_LEAD), and
clearly better inside the favorable gate than outside. A context flip alone
is not a reject — it is the very conditionality the thesis predicts.

Diagnostic only. Does NOT modify scoring.
"""
import os, time, datetime, statistics
from collections import defaultdict
from statistics import mean, median
import urllib.request, zipfile, io

from altcoin.analyzer import _compute_volume_metrics  # ACTUAL engine function

BASE = "https://data.binance.vision/data/spot/monthly/klines"
MAJORS = ["BTCUSDT", "ETHUSDT"]           # BTC + ETH co-lead (ETH is NOT an alt)
ALTS = ["SOLUSDT", "LINKUSDT", "AAVEUSDT", "UNIUSDT", "CRVUSDT", "SNXUSDT",
        "COMPUSDT", "MKRUSDT", "DOGEUSDT", "RAYUSDT", "DYDXUSDT", "LDOUSDT"]
COINS = MAJORS + ALTS
HORIZON = 7
CACHE = "/tmp/bv_monthly_cache"
os.makedirs(CACHE, exist_ok=True)


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


def _median(xs):
    xs = [x for x in xs if x is not None]
    return median(xs) if xs else None


def majors_ret30(idx):
    return _median([ret30(s, idx) for s in MAJORS])


def alts_ret30(idx):
    return _median([ret30(s, idx) for s in ALTS])


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


# ---- Build per-(date) observations: token signal + context state ----
# context states (the GATE):
#   majors_ctx : RISK_ON  majors_ret30 > +0.05
#                RISK_OFF majors_ret30 < -0.05
#                NEUTRAL  otherwise
#   rotation   : ALT_LEAD  (alts_ret30 - majors_ret30) > +0.05  => money rotating into alts
#                MAJ_LEAD  < -0.05                                => majors absorbing
#                FLAT      otherwise
#   FAVORABLE_GATE = RISK_ON majors AND ALT_LEAD rotation
rows_by_ctx = defaultdict(list)   # (majors_ctx, rotation) -> list of rows
for i in range(40, len(btc_dates)):
    t = btc_dates[i]
    if i + HORIZON >= len(btc_dates):
        break
    t_fwd = btc_dates[i + HORIZON]
    mr = majors_ret30(i)
    ar = alts_ret30(i)
    if mr is None or ar is None:
        continue
    rot = ar - mr
    mctx = "RISK_ON" if mr > 0.05 else ("RISK_OFF" if mr < -0.05 else "NEUTRAL")
    rotc = "ALT_LEAD" if rot > 0.05 else ("MAJ_LEAD" if rot < -0.05 else "FLAT")
    for sym in ALTS:
        p1 = prices[sym].get(t); pf = prices[sym].get(t_fwd)
        if not p1 or not pf or p1 <= 0:
            continue
        fwd = pf / p1 - 1
        vm = vol_metrics_at(sym, i)
        if not vm or vm["vol_ratio"] is None or vm["vol_trend"] is None:
            continue
        vr, vt = vm["vol_ratio"], vm["vol_trend"]
        prox = prox_30d_high(sym, i)
        if prox is None:
            continue
        rows_by_ctx[(mctx, rotc)].append({
            "fwd": fwd, "vol_ratio": vr, "vol_trend": vt, "prox": prox,
            "early": vr - 2.0 * prox,            # ∝ Volume Expansion − Price Response
            "early_trig": vr >= 1.2 and prox < 0.5,
            "early_strong": vr >= 1.3 and prox < 0.5,
            "chase_trig": vr >= 1.2 and prox >= 0.5,   # volume up AFTER price ran
        })

# aggregate context date counts
from collections import Counter
ctx_dates = Counter()
for (mctx, rotc), rows in rows_by_ctx.items():
    pass  # rows carry no date; recompute below via loop not needed — report by ctx

print("\n=== CASCADE CONTEXT GATE — observation counts per state ===")
print(f"{'majors':9} {'rotation':9} {'rows':>8}  {'= FAVORABLE_GATE?'}")
print("-" * 56)
fav = (mctx, rotc) if False else None
for (mctx, rotc), rows in rows_by_ctx.items():
    is_fav = (mctx == "RISK_ON" and rotc == "ALT_LEAD")
    mark = " <== FAVORABLE" if is_fav else ""
    print(f"{mctx:9} {rotc:9} {len(rows):>8}  {mark}")

def ctx_label(rows_fav):
    return "FAVORABLE-GATE" if rows_fav else "other"

def ic_for(rows, key):
    return spearman([r[key] for r in rows], [r["fwd"] for r in rows])

def hit_rate(rows, pred):
    n = sum(1 for r in rows if pred(r))
    return (sum(1 for r in rows if pred(r) and r["fwd"] > 0) / n, n) if n else (None, 0)

print("\n=== TOKEN SIGNAL IC vs 7d FORWARD RETURN, per context state ===")
print(f"{'majors':9} {'rotation':9} {'n':>6} {'earlyIC':>8} {'qiflow(vt)IC':>12}")
print("-" * 56)
for (mctx, rotc), rows in rows_by_ctx.items():
    if len(rows) < 20:
        continue
    eic = ic_for(rows, "early")
    qic = ic_for(rows, "vol_trend")
    print(f"{mctx:9} {rotc:9} {len(rows):>6} {str(eic):>8} {str(qic):>12}")

# ---- THE KEY TEST: signal inside vs outside favorable gate ----
fav_rows = [r for (m, rt), rows in rows_by_ctx.items()
            if m == "RISK_ON" and rt == "ALT_LEAD" for r in rows]
oth_rows = [r for (m, rt), rows in rows_by_ctx.items()
            if not (m == "RISK_ON" and rt == "ALT_LEAD") for r in rows]

print("\n=== MONEY-BEFORE-PRICE: early-accumulation vs breakout-chasing, by gate ===")
print(f"{'gate':15} {'n':>6} {'earlyHit':>9} {'chaseHit':>9} {'earlyIC':>8} {'gap(vs chase)':>13}")
print("-" * 66)
for lbl, rows in [("FAVORABLE-GATE", fav_rows), ("NOT-FAVORABLE", oth_rows)]:
    if not rows:
        continue
    eh, en = hit_rate(rows, lambda r: r["early_trig"])
    ch, cn = hit_rate(rows, lambda r: r["chase_trig"])
    es, ss = hit_rate(rows, lambda r: r["early_strong"])
    eic = ic_for(rows, "early")
    gap = (eh - ch) if (eh is not None and ch is not None) else None
    print(f"{lbl:15} {len(rows):>6} {str(eh and round(eh,3)):>9} {str(ch and round(ch,3)):>9} "
          f"{str(eic):>8} {str(gap and round(gap,3)):>13}")

print("\n=== STRONG EARLY-ACCUMULATION trigger hit-rate by gate (vol_ratio>=1.3, prox<0.5) ===")
for lbl, rows in [("FAVORABLE-GATE", fav_rows), ("NOT-FAVORABLE", oth_rows)]:
    if not rows:
        continue
    h, n = hit_rate(rows, lambda r: r["early_strong"])
    base_h, base_n = hit_rate(rows, lambda r: True)
    gap = (h - base_h) if (h is not None and base_h is not None) else None
    print(f"{lbl:15} strong n={n:>4} hit={h and round(h,3)}  base(n={base_n}) hit={base_h and round(base_h,3)}  gap={gap and round(gap,3):+.3f}")

print("""
INTERPRETATION (CASCADE / MONEY-BEFORE-PRICE):
- The thesis predicts the token volume signal fires best when the gate is open
  (majors RISK_ON + rotation ALT_LEAD). Check whether early-accumulation's IC
  and hit-rate are clearly BETTER inside FAVORABLE-GATE than outside.
- 'earlyHit vs chaseHit': if early-accumulation (volume up, price still low)
  beats breakout-chasing (volume up after price ran) — that is the
  money-before-price edge. If chase >= early, volume is a LAGGING not leading
  signal and the thesis's lead premise fails.
- Order-flow components (net buy, smart-wallet, exchange flows, liquidity
  growth) are NOT in this data and remain unvalidated.
""")

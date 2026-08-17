"""Cross-era cross-sectional momentum test on Binance Vision monthly klines.

Downloads 1d monthly klines for long-history coins, reconstructs daily close
series, and tests cross-sectional momentum (lookback L -> forward H) PER ERA
(bull / bear / sideways by BTC 30d trend), with era-split so we can see whether
momentum is consistently signed across regimes or only works/only fails in one.

Diagnostic only. Streams per-file (never loads all history at once) to respect
the box's 1.3G available RAM.
"""
import urllib.request, urllib.parse, zipfile, io, csv, os, time
from collections import defaultdict
from statistics import mean, median

BASE = "https://data.binance.vision/data/spot/monthly/klines"
COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT", "AAVEUSDT", "UNIUSDT",
         "CRVUSDT", "SNXUSDT", "COMPUSDT", "MKRUSDT", "DOGEUSDT", "RAYUSDT",
         "DYDXUSDT", "LDOUSDT"]
LOOKBACKS = [7, 14, 30, 60]
HORIZON = 7
CACHE = "/tmp/bv_monthly_cache"
os.makedirs(CACHE, exist_ok=True)

def _normalize_ts(ts):
    """Binance Vision monthly klines have INCONSISTENT timestamp units:
    old files (2017-2018) use milliseconds, newer files (2025+) use
    microseconds (1000x). Normalize to milliseconds so chronology is sane.
    2026 epoch ~= 1.78e9 s = 1.78e12 ms = 1.78e15 us."""
    if ts > 1e14:          # microseconds
        return ts / 1000
    if ts > 1e11:          # milliseconds (normal case)
        return ts
    return ts * 1000       # seconds

def fetch_month(sym, year, month):
    """Return list of daily klines [open_time_ms, close, ...] for one month, cached."""
    fn = f"{CACHE}/{sym}-{year}-{month:02d}.csv"
    if os.path.exists(fn):
        return _read_csv(fn)
    url = f"{BASE}/{sym}/1d/{sym}-1d-{year:04d}-{month:02d}.zip"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/7.8"})
        data = urllib.request.urlopen(req, timeout=40).read()
    except Exception:
        return None  # month not available (not yet listed / not ended)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        name = z.namelist()[0]
        raw = z.read(name).decode()
    rows = []
    for line in raw.strip().splitlines():
        p = line.split(",")
        # kline: [0]open_time,[1]open,[2]high,[3]low,[4]close,[5]vol,[6]close_time,...
        try:
            t = _normalize_ts(int(p[0])); close = float(p[4])
        except (ValueError, IndexError):
            continue
        rows.append((int(t), close))
    with open(fn, "w") as f:
        for t, close in rows:
            f.write(f"{t},{close}\n")
    return rows

def _read_csv(fn):
    rows = []
    with open(fn) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            t, close = line.split(",")
            rows.append((_normalize_ts(int(t)), float(close)))
    return rows

# --- Build daily close series per symbol, merged & date-ordered ---
import datetime
def _fmt(ts):
    return datetime.datetime.fromtimestamp(ts/1000, datetime.timezone.utc).date().isoformat()

# We collect (date_ts, close) across all months, then sort. Memory-safe:
# each symbol series is tiny (~9yr * 365 = ~3300 points).
series = {}
for sym in COINS:
    by_ts = {}
    # year range: sym-dependent, just scan 2017..2026
    for year in range(2017, 2027):
        for month in range(1, 13):
            rows = fetch_month(sym, year, month)
            if rows:
                for t, c in rows:
                    by_ts[t] = c
            # polite pacing but cache makes repeat instant
            time.sleep(0.02)
    series[sym] = sorted(by_ts.items())
    print(f"  {sym}: {len(series[sym])} daily closes  {_fmt(series[sym][0][0])}..{_fmt(series[sym][-1][0])}")

# --- Build price matrix by (date_index, symbol) using BTC date grid ---
# Use union of dates where BTC exists as the timeline.
btc_dates = [t for t, _ in series["BTCUSDT"]]
date_index = {t: i for i, t in enumerate(btc_dates)}
# map symbol->{date_ts: close}
prices = {}
for sym, s in series.items():
    prices[sym] = dict(s)

def price_at(sym, ts):
    d = prices.get(sym)
    return d.get(ts) if d else None

def spearman(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    def ranks(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0.0]*len(vals); i = 0
        while i < len(order):
            j = i
            while j+1 < len(order) and vals[order[j+1]] == vals[order[i]]:
                j += 1
            avg = (i+j)/2.0+1
            for k in range(i, j + 1): r[order[k]] = avg
            i = j+1
        return r
    rx, ry = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    n = len(pairs); mx = sum(rx)/n; my = sum(ry)/n
    cov = sum((a-mx)*(b-my) for a,b in zip(rx,ry))
    vx = sum((a-mx)**2 for a in rx); vy = sum((b-my)**2 for b in ry)
    if vx <= 1e-12 or vy <= 1e-12: return None
    return cov/(vx**0.5*vy**0.5)

# --- Classify each date's regime by BTC 30d return ---
def btc_ret30(idx):
    i0 = max(0, idx-30)
    p0 = price_at("BTCUSDT", btc_dates[i0]); p1 = price_at("BTCUSDT", btc_dates[idx])
    if p0 and p1 and p0 > 0: return p1/p0 - 1
    return None

# --- Per-regime cross-sectional momentum ---
regimes = defaultdict(list)  # regime -> list of IC per date
for i in range(40, len(btc_dates)):
    t = btc_dates[i]
    if i + HORIZON >= len(btc_dates): break
    t_fwd = btc_dates[i + HORIZON]
    r30 = btc_ret30(i)
    if r30 is None: continue
    regime = "BULL" if r30 > 0.15 else ("BEAR" if r30 < -0.15 else "SIDEWAYS")
    for L in LOOKBACKS:
        i0 = i - L
        if i0 < 0: continue
        t0 = btc_dates[i0]
        rows = []
        for sym in COINS:
            p0 = price_at(sym, t0); p1 = price_at(sym, t); pf = price_at(sym, t_fwd)
            if p0 and p1 and pf and p0 > 0 and p1 > 0:
                trail = p1/p0 - 1; fwd = pf/p1 - 1
                rows.append((trail, fwd))
        if len(rows) < 8: continue
        ic = spearman([a for a,_ in rows], [b for _,b in rows])
        if ic is not None:
            regimes[(regime, L)].append(ic)

print("\n=== CROSS-ERA CROSS-SECTIONAL MOMENTUM (IC per date, pooled per era) ===")
print(f"{'regime':9} {'L':>3} {'dates':>6} {'mean IC':>9} {'IC>0 frac':>10} {'median IC':>9}")
print("-"*54)
for regime in ["BULL", "SIDEWAYS", "BEAR"]:
    for L in LOOKBACKS:
        ics = regimes.get((regime, L), [])
        if not ics:
            print(f"{regime:9} {L:>3} {'0':>6} {'n/a':>9} {'n/a':>10} {'n/a':>9}")
            continue
        pos = sum(1 for x in ics if x > 0)/len(ics)
        print(f"{regime:9} {L:>3} {len(ics):>6} {mean(ics):>9.3f} {pos:>10.2%} {median(ics):>9.3f}")

print("""
INTERPRETATION:
- mean IC > +0.03 AND IC>0 fraction clearly above 0.55 in MULTIPLE eras => real cross-sectional edge
- positive only in BULL (not sideways/bear) => momentum works only in trending-up regimes (expected)
- flat/negative everywhere => momentum genuinely dead for altcoins, era-independent
""")

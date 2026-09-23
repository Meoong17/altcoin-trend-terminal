"""Model audit — cross-sectional information content of the LIVE scoring stack
on the accumulated point-in-time history (history.db snapshots).

Answers, empirically and without lookahead:
  1. Which score/component actually ranks coins? (mean cross-sectional
     Spearman IC vs forward 7d return + IC>0 fraction)
  2. Is it era-stable? (3 chronological eras) and regime-conditional?
  3. Are components redundant (double counting)? (mean pairwise corr)
  4. How many folds/rows actually carry each component (coverage over time,
     i.e. how much of history is under the current formula)?
  5. entry_grade / VaF / OTF — do the displayed grades separate winners?

Prices/labels come from the SAME point-in-time snapshots (`latest_price`),
sequence-based like altcoin/backtest.py forward_return (no lookahead, no
survivorship patch).

Usage: .venv/bin/python analysis/model_audit.py [--horizon 7] [--json out.json]
"""
import argparse
import json
import sqlite3
import statistics
from collections import defaultdict

from altcoin.history import _conn

COMPONENTS = ["flow_rotation", "participation", "rel_strength",
              "compression", "confirmation"]
HORIZON = 7


def load():
    c = _conn()
    rows = c.execute("SELECT date, symbol, payload FROM snapshots").fetchall()
    snaps = {}          # (symbol,date) -> dict
    dates = set()
    for date, symbol, payload in rows:
        try:
            p = json.loads(payload)
        except ValueError:
            continue
        d = p.get("trend_score_detail") or {}
        comps = {}
        for dr in (d.get("drivers") or []):
            comps[dr.get("component")] = dr.get("value")
        et = p.get("entry_timing") or {}
        vaf = p.get("vaf") or {}
        recon = p.get("market_regime_reconstructed") or {}
        snaps[(symbol, date)] = {
            "price": p.get("latest_price"),
            "trend_score": p.get("trend_score"),
            "comps": comps,
            "coverage": (d.get("coverage") or {}).get("weight_covered"),
            "version": d.get("version"),
            "model_version": p.get("model_version"),
            "regime": p.get("market_regime"),
            "regime_recon": recon.get("state"),
            "otf": et.get("otf"),
            "grade": et.get("grade"),
            "vaf": vaf.get("vaf"),
            "fees30": ((p.get("fundamental_raw") or {}).get("fees_30d")),
        }
        dates.add(date)
    return snaps, sorted(dates)


def forward_returns(snaps, dates, horizon):
    """(symbol,date) -> forward return using the symbol's own sorted date seq."""
    idx = {d: i for i, d in enumerate(dates)}
    by_sym = defaultdict(list)
    for (sym, d) in snaps:
        by_sym[sym].append(d)
    fr = {}
    for sym, ds in by_sym.items():
        ds.sort()
        for i, d in enumerate(ds):
            if i + horizon >= len(ds):
                continue
            p0 = snaps[(sym, d)]["price"]
            p1 = snaps[(sym, ds[i + horizon])]["price"]
            if p0 and p1 and p0 > 0 and p1 > 0:
                fr[(sym, d)] = p1 / p0 - 1
    return fr


def spearman(xs, ys):
    n = len(xs)
    if n < 3:
        return None

    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def per_date_ic(snaps, fr, key, min_coins=5):
    """key: callable(snap)->value. Returns list of (date, n, ic)."""
    by_date = defaultdict(list)
    for (sym, d), s in snaps.items():
        if (sym, d) not in fr:
            continue
        v = key(s)
        if v is None:
            continue
        by_date[d].append((v, fr[(sym, d)]))
    out = []
    for d, rows in by_date.items():
        if len(rows) < min_coins:
            continue
        ic = spearman([r[0] for r in rows], [r[1] for r in rows])
        if ic is not None:
            out.append((d, len(rows), ic))
    return out


def summarize(ics):
    if not ics:
        return None
    vals = [ic for _, _, ic in ics]
    return {
        "days": len(vals),
        "mean_ic": round(statistics.mean(vals), 4),
        "median_ic": round(statistics.median(vals), 4),
        "frac_ic_pos": round(sum(1 for v in vals if v > 0) / len(vals), 3),
        "stdev": round(statistics.pstdev(vals), 4),
        "t_stat": round(statistics.mean(vals) /
                        (statistics.pstdev(vals) / len(vals) ** 0.5), 2)
        if statistics.pstdev(vals) > 0 else None,
    }


def era_split(ics, dates, n_era=3):
    bounds = [dates[int(len(dates) * i / n_era)] for i in range(1, n_era)]
    eras = [[] for _ in range(n_era)]
    for d, n, ic in ics:
        k = sum(1 for b in bounds if d >= b)
        eras[k].append((d, n, ic))
    return [summarize(e) for e in eras]


def modal_regime(snaps):
    """{date: regime} using the RECONSTRUCTED regime when present, else the
    stored one, by majority over the coins in that date's snapshot."""
    per_date = defaultdict(lambda: defaultdict(int))
    for (sym, d), s in snaps.items():
        reg = (s.get("regime_recon") or s.get("regime"))
        if reg:
            per_date[d][reg] += 1
    return {d: max(cnt.items(), key=lambda kv: kv[1])[0]
            for d, cnt in per_date.items()}


def regime_split(ics, snaps):
    # Regime per (symbol,date): prefer the RECONSTRUCTED regime
    # (`market_regime_reconstructed`, written by analysis/regime_backfill.py
    # for historical rows the collector never labeled) over the stored one.
    # Without this, 212 of 254 IC days collapse into UNKNOWN and every
    # regime-conditional question is unanswerable. The reconstructed value
    # is a separate field on purpose — the original point-in-time record is
    # never overwritten.
    reg_of_date = modal_regime(snaps)
    out = defaultdict(list)
    for d, n, ic in ics:
        out[reg_of_date.get(d, "UNKNOWN")].append((d, n, ic))
    return {k: summarize(v) for k, v in out.items() if len(v) >= 10}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=HORIZON)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    snaps, dates = load()
    fr = forward_returns(snaps, dates, a.horizon)
    print(f"snapshots={len(snaps)} dates={len(dates)} ({dates[0]}..{dates[-1]}) "
          f"rows_with_{a.horizon}d_forward={len(fr)}")

    # component coverage over time
    print("\n=== COVERAGE OVER TIME (rows carrying each component) ===")
    by_month = defaultdict(lambda: defaultdict(int))
    tot_month = defaultdict(int)
    for (sym, d), s in snaps.items():
        m = d[:7]
        tot_month[m] += 1
        for k in COMPONENTS:
            if s["comps"].get(k) is not None:
                by_month[m][k] += 1
    for m in sorted(tot_month):
        row = " ".join(f"{k[:6]}={by_month[m][k]}/{tot_month[m]}" for k in COMPONENTS)
        print(f"  {m} n={tot_month[m]:5}  {row}")

    print("\n=== VERSION TAG OVER TIME (model_version) ===")
    vm = defaultdict(lambda: defaultdict(int))
    for (sym, d), s in snaps.items():
        vm[d[:7]][str(s["model_version"])[:12]] += 1
    for m in sorted(vm):
        top = sorted(vm[m].items(), key=lambda kv: -kv[1])[:3]
        print(f"  {m}: {top}")

    # IC table
    targets = [("trend_score", lambda s: s["trend_score"])]
    for k in COMPONENTS:
        targets.append((k, (lambda kk: (lambda s: s["comps"].get(kk)))(k)))
    targets += [("otf", lambda s: s["otf"]), ("vaf", lambda s: s["vaf"]),
                ("grade_rank", lambda s: {"A+": 4, "A": 3, "B": 2, "Avoid": 1}.get(s["grade"]))]

    report = {"horizon": a.horizon, "dates": [dates[0], dates[-1]],
              "rows": len(fr), "targets": {}}
    print(f"\n=== CROSS-SECTIONAL IC vs forward {a.horizon}d return ===")
    print(f"{'target':16} {'days':>5} {'meanIC':>8} {'frac>0':>7} {'t':>6}  era1     era2     era3")
    for name, fn in targets:
        ics = per_date_ic(snaps, fr, fn)
        s = summarize(ics)
        if not s:
            continue
        eras = era_split(ics, dates)
        rep = {"overall": s, "eras": eras, "by_regime": regime_split(ics, snaps)}
        report["targets"][name] = rep
        es = "  ".join(f"{e['mean_ic']:+.3f}" if e else "  n/a  " for e in eras)
        flag = "" if abs(s["mean_ic"]) < 0.02 or (s["t_stat"] or 0) < 2 else \
               ("  <== EDGE" if s["mean_ic"] > 0 else "  <== NEG")
        print(f"{name:16} {s['days']:5} {s['mean_ic']:+8.4f} {s['frac_ic_pos']:7.3f} "
              f"{str(s['t_stat']):>6}  {es}{flag}")

    print("\n=== IC BY REGIME (modal date regime) ===")
    for name, fn in targets:
        r = report["targets"].get(name, {}).get("by_regime") or {}
        if not r:
            continue
        print(f"  {name:16} " + "  ".join(
            f"{k}:{v['mean_ic']:+.3f}(n={v['days']},f={v['frac_ic_pos']:.2f})"
            for k, v in sorted(r.items())))

    # Regime-conditional results are only interesting if they are STABLE
    # inside the regime, not one lucky stretch. Split each (target, regime)
    # cell's days into two chronological halves and report both means: a
    # sign that flips between halves is a regime-conditional artifact, not
    # a regime-conditional edge.
    print("\n=== REGIME-CONDITIONAL ERA STABILITY (chronological halves) ===")
    reg_of_date = modal_regime(snaps)
    for name, fn in targets:
        ics = per_date_ic(snaps, fr, fn)
        cells = defaultdict(list)
        for d, n, ic in ics:
            cells[reg_of_date.get(d, "UNKNOWN")].append((d, ic))
        for reg, obs in sorted(cells.items()):
            if len(obs) < 20:
                continue
            obs.sort()
            h = len(obs) // 2
            h1 = statistics.mean([ic for _, ic in obs[:h]])
            h2 = statistics.mean([ic for _, ic in obs[h:]])
            stable = "stable" if (h1 > 0) == (h2 > 0) else "SIGN FLIP"
            print(f"  {name:16} {reg:22} n={len(obs):3} half1={h1:+.3f} "
                  f"half2={h2:+.3f}  {stable}")
            report.setdefault("regime_stability", {})[f"{name}|{reg}"] = {
                "n_days": len(obs), "half1": round(h1, 4), "half2": round(h2, 4),
                "stable": stable}

    # redundancy: mean cross-sectional corr between components
    print("\n=== COMPONENT REDUNDANCY (mean pairwise Spearman, same day) ===")
    pairs = [(COMPONENTS[i], COMPONENTS[j])
             for i in range(len(COMPONENTS)) for j in range(i + 1, len(COMPONENTS))]
    by_date = defaultdict(list)
    for (sym, d), s in snaps.items():
        by_date[d].append(s["comps"])
    red = {}
    for c1, c2 in pairs:
        vals = []
        for d, rows in by_date.items():
            xs = [(r.get(c1), r.get(c2)) for r in rows]
            xs = [(x, y) for x, y in xs if x is not None and y is not None]
            if len(xs) >= 8:
                ic = spearman([x for x, _ in xs], [y for _, y in xs])
                if ic is not None:
                    vals.append(ic)
        if vals:
            red[f"{c1}|{c2}"] = round(statistics.mean(vals), 3)
    for k, v in sorted(red.items(), key=lambda kv: -abs(kv[1])):
        print(f"  {k:38} {v:+.3f}")
    report["redundancy"] = red

    if a.json:
        with open(a.json, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nwrote {a.json}")


if __name__ == "__main__":
    main()

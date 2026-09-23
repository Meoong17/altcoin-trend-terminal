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
        exp = ((p.get("experimental") or {}).get("candidates") or {})
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
            "exp": exp,
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
    # Parallel v4 candidates (not adopted — recorded for comparison).
    for k in ("accel_price", "accel_vol", "accel_flow", "transition",
              "entry_decoupled", "v4_equal"):
        targets.append(("exp_" + k,
                        (lambda kk: (lambda s: (s.get("exp") or {}).get(kk)))(k)))

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

    # ── EXTENSIONS (2026-09-23 external review, item 2) ────────────────
    # (a) multi-horizon IC, (b) quantile / long-short spread, (c) PATH
    # metrics (MFE/MAE/up-before-down). A single horizon-7 mean IC cannot
    # distinguish "cannot pick winners" from "usefully avoids losers", and
    # a terminal forward return hides whether a setup was ever tradable
    # (a +15% 7d return that first went -8% is not the same trade signal as
    # one that never drew down).
    main_targets = [("trend_score", lambda s: s["trend_score"]),
                    ("flow_rotation", lambda s: s["comps"].get("flow_rotation")),
                    ("participation", lambda s: s["comps"].get("participation")),
                    ("confirmation", lambda s: s["comps"].get("confirmation")),
                    ("rel_strength", lambda s: s["comps"].get("rel_strength")),
                    ("otf", lambda s: s["otf"]),
                    ("vaf", lambda s: s["vaf"])]
    # Parallel v4 candidates (altcoin/experimental.py): recorded by the
    # collector precisely so they get the SAME test as the live scores on the
    # same rows. They are not adopted — this is the evidence that would decide.
    main_targets += [("exp_" + k, (lambda kk: (lambda s: (s.get("exp") or {}).get(kk)))(k))
                     for k in ("accel_price", "accel_vol", "accel_flow",
                               "transition", "entry_decoupled", "v4_equal")]

    print("\n=== MULTI-HORIZON CROSS-SECTIONAL IC (mean / frac>0 / days) ===")
    print(f"{'target':16} " + " ".join(f"{('h=%d' % h):>18}" for h in HORIZONS))
    mh = {}
    for name, fn in main_targets:
        cells = []
        for h in HORIZONS:
            fr_h = fr if h == a.horizon else forward_returns(snaps, dates, h)
            s = summarize(per_date_ic(snaps, fr_h, fn))
            mh.setdefault(name, {})[h] = s
            cells.append(f"{s['mean_ic']:+.3f}/{s['frac_ic_pos']:.2f}/{s['days']:>3}"
                         if s else "        n/a       ")
        print(f"{name:16} " + " ".join(f"{c:>18}" for c in cells))
    report["multi_horizon_ic"] = mh

    print("\n=== QUANTILE / LONG-SHORT SPREAD (h=%d, per-date cross-section) ==="
          % a.horizon)
    print(f"{'target':16} {'top10%':>9} {'top20%':>9} {'bot20%':>9} "
          f"{'spread':>9} {'t':>6} {'days+':>6}")
    qrep = {}
    for name, fn in main_targets:
        q = quantile_spread(snaps, fr, fn)
        qrep[name] = q
        if not q:
            continue
        print(f"{name:16} {q['top10']:+9.4f} {q['top20']:+9.4f} "
              f"{q['bot20']:+9.4f} {q['spread']:+9.4f} {q['spread_t']:>6} "
              f"{q['frac_days_pos']:>6.3f}")
    report["quantile_spread"] = qrep

    print("\n=== PATH METRICS BY SCORE BUCKET (top vs bottom quintile) ===")
    print("  h  target          bucket      n   medMFE   medMAE   MFE/|MAE|  "
          "P(+5%<->-5%)  medDaysMFE  medDaysMAE")
    prep = {}
    for h in (7, 14):
        fr_h = fr if h == a.horizon else forward_returns(snaps, dates, h)
        paths = path_stats(snaps, h)
        for name, fn in main_targets:
            buckets = bucket_paths(snaps, fr_h, paths, fn)
            for bname, st in buckets.items():
                if not st or st["n"] < 30:
                    continue
                prep.setdefault(name, {})[f"h{h}_{bname}"] = st
                rr = f"{st['rr']:.2f}" if st["rr"] is not None else "n/a"
                upf = (f"{st['up_first_p']:.3f}"
                       if st["up_first_p"] is not None else "n/a")
                print(f"  {h:<2} {name:16} {bname:10} {st['n']:5} "
                      f"{st['med_mfe']:+8.3f} {st['med_mae']:+8.3f} "
                      f"{rr:>9} {upf:>12} "
                      f"{st['med_tt_mfe']:>10} {st['med_tt_mae']:>11}")
    report["path_metrics"] = prep

    if a.json:
        with open(a.json, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nwrote {a.json}")


# ── Extension helpers (item 2 of the 2026-09-23 external review) ──
HORIZONS = (1, 3, 7, 14, 30)


def series_by_symbol(snaps):
    """{symbol: [(date, price)] ascending} — each coin's own observable
    sequence, so path metrics never borrow a price from another coin's grid."""
    by_sym = defaultdict(list)
    for (sym, d), s in snaps.items():
        p = s.get("price")
        if p and p > 0:
            by_sym[sym].append((d, p))
    for sym in by_sym:
        by_sym[sym].sort()
    return by_sym


def path_stats(snaps, horizon, up=0.05, dn=-0.05):
    """{(symbol,date): path metrics over the next `horizon` observations}.

    mfe/mae      max favourable / adverse excursion vs entry price
    tt_mfe/mae   observations until that extreme
    up_first     True if +5% printed before -5%, False if the drawdown came
                 first, None if neither level was touched in the window
    """
    out = {}
    for sym, seq in series_by_symbol(snaps).items():
        px = [p for _, p in seq]
        for i in range(len(seq)):
            if i + horizon >= len(px):
                continue
            p0 = px[i]
            window = px[i + 1:i + 1 + horizon]
            if not window:
                continue
            hi = max(window)
            lo = min(window)
            tt_up = next((j + 1 for j, x in enumerate(window)
                          if x / p0 - 1 >= up), None)
            tt_dn = next((j + 1 for j, x in enumerate(window)
                          if x / p0 - 1 <= dn), None)
            up_first = None if (tt_up is None and tt_dn is None) else (
                tt_up is not None and (tt_dn is None or tt_up < tt_dn))
            out[(sym, seq[i][0])] = {
                "mfe": hi / p0 - 1, "mae": lo / p0 - 1,
                "tt_mfe": window.index(hi) + 1, "tt_mae": window.index(lo) + 1,
                "up_first": up_first,
            }
    return out


def quantile_spread(snaps, fr, key, top_q=0.10, top2_q=0.20, bot_q=0.20,
                    min_coins=8):
    """Mean forward return of the top-decile / top-quintile / bottom-quintile
    by score, per date, then averaged across dates (equal weight per day, so
    a busy day cannot dominate). Also the top20-bottom20 spread and the
    fraction of days it is positive."""
    by_date = defaultdict(list)
    for (sym, d), s in snaps.items():
        if (sym, d) not in fr:
            continue
        v = key(s)
        if v is None:
            continue
        by_date[d].append((v, fr[(sym, d)]))
    tops, tops2, bots, spreads = [], [], [], []
    for d, rows in by_date.items():
        if len(rows) < min_coins:
            continue
        rows.sort(key=lambda r: -r[0])
        n = len(rows)
        k1 = max(1, int(round(n * top_q)))
        k2 = max(1, int(round(n * top2_q)))
        kb = max(1, int(round(n * bot_q)))
        tops.append(statistics.mean([r[1] for r in rows[:k1]]))
        t2 = statistics.mean([r[1] for r in rows[:k2]])
        b2 = statistics.mean([r[1] for r in rows[-kb:]])
        tops2.append(t2)
        bots.append(b2)
        spreads.append(t2 - b2)
    if len(spreads) < 5:
        return None
    sd = statistics.pstdev(spreads) or 1e-12
    return {
        "top10": round(statistics.mean(tops), 4),
        "top20": round(statistics.mean(tops2), 4),
        "bot20": round(statistics.mean(bots), 4),
        "spread": round(statistics.mean(spreads), 4),
        "spread_t": round(statistics.mean(spreads) / (sd / len(spreads) ** 0.5), 2),
        "frac_days_pos": round(sum(1 for x in spreads if x > 0) / len(spreads), 3),
        "days": len(spreads),
    }


def bucket_paths(snaps, fr, paths, key, min_coins=8):
    """Path metrics for the top-quintile vs bottom-quintile by score,
    pooled across all (coin,date) rows in those buckets."""
    by_date = defaultdict(list)
    for (sym, d), s in snaps.items():
        if (sym, d) not in fr or (sym, d) not in paths:
            continue
        v = key(s)
        if v is None:
            continue
        by_date[d].append((v, (sym, d)))
    top_rows, bot_rows = [], []
    for d, rows in by_date.items():
        if len(rows) < min_coins:
            continue
        rows.sort(key=lambda r: -r[0])
        k = max(1, int(round(len(rows) * 0.20)))
        top_rows += [k_ for _, k_ in rows[:k]]
        bot_rows += [k_ for _, k_ in rows[-k:]]

    def agg(keys_):
        ps = [paths[k_] for k_ in keys_]
        if len(ps) < 30:
            return None
        med = lambda xs: statistics.median(xs)
        mfe = med([p["mfe"] for p in ps])
        mae = med([p["mae"] for p in ps])
        touched = [p["up_first"] for p in ps if p["up_first"] is not None]
        return {
            "n": len(ps),
            "med_mfe": round(mfe, 4), "med_mae": round(mae, 4),
            "rr": round(mfe / abs(mae), 2) if mae else None,
            "up_first_p": (round(sum(1 for x in touched if x) / len(touched), 3)
                           if touched else None),
            "med_tt_mfe": med([p["tt_mfe"] for p in ps]),
            "med_tt_mae": med([p["tt_mae"] for p in ps]),
        }
    return {"top20": agg(top_rows), "bot20": agg(bot_rows)}


if __name__ == "__main__":
    main()
